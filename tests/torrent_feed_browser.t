use strict;
use warnings;
no warnings 'once';
use utf8;
use Test::More;
use FindBin;
use File::Temp qw(tempdir);
use JSON::PP qw(encode_json);
use IPC::Open3;
use Symbol qw(gensym);
use URI;
use URI::file;

require "$FindBin::Bin/../torrent_feed_browser.pl";

sub app { my ($name, @args) = @_; no strict 'refs'; return &{"TorrentFeedBrowser::$name"}(@args); }
sub fails {
    my ($name, $pattern, $code) = @_;
    eval { $code->(); 1 };
    my $error = ref($@) eq 'HASH' ? $@->{message} : $@;
    like($error, $pattern, $name);
}

{
    no warnings 'redefine';
    local %ENV = %ENV;
    delete @ENV{qw(PERL_LWP_SSL_CA_FILE HTTPS_CA_FILE PERL_LWP_SSL_CA_PATH HTTPS_CA_DIR PERL_LWP_SSL_VERIFY_HOSTNAME)};
    my (%ssl_options, $default_ca_calls);
    $default_ca_calls = 0;
    local *IO::Socket::SSL::default_ca = sub {
        $default_ca_calls++;
        return (SSL_ca_file => '/system/ca.pem');
    };
    local *LWP::UserAgent::get = sub {
        my ($ua) = @_;
        %ssl_options = map { $_ => $ua->ssl_opts($_) } $ua->ssl_opts;
        return HTTP::Response->new(200, 'OK', [], 'feed contents');
    };
    is(app('fetch_feed', 'https://example.com/feed.xml', 20), 'feed contents', 'HTTPS fetch returns response content');
    is($ssl_options{SSL_ca_file}, '/system/ca.pem', 'HTTPS explicitly uses detected system certificates');
    is($ssl_options{verify_hostname}, 1, 'HTTPS hostname verification stays enabled');
    ok(!exists($ssl_options{SSL_verify_mode}) || $ssl_options{SSL_verify_mode}, 'HTTPS certificate verification is not disabled');

    for my $override (
        [PERL_LWP_SSL_CA_FILE => 'SSL_ca_file'],
        [HTTPS_CA_FILE => 'SSL_ca_file'],
        [PERL_LWP_SSL_CA_PATH => 'SSL_ca_path'],
        [HTTPS_CA_DIR => 'SSL_ca_path'],
    ) {
        local $ENV{$override->[0]} = '/custom/trust';
        app('fetch_feed', 'https://example.com/feed.xml', 20);
        is($ssl_options{$override->[1]}, '/custom/trust', "HTTPS preserves $override->[0]");
    }
    is($default_ca_calls, 1, 'explicit CA overrides skip system certificate detection');
}

my $rss = <<'XML';
<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>
<item><title>Skipped</title><link>https://example.com/</link></item>
<item><title>Café &amp; Straße</title><pubDate>2026-09-20</pubDate><enclosure url="magnet:?xt=urn:btih:ONE&amp;dn=Cafe"/></item>
<item><title>Second Show</title><content:encoded><![CDATA[<a href="magnet:?xt=urn:btih:TWO&amp;dn=Second">link</a>]]></content:encoded></item>
<item><description>magnet:?xt=urn:btih:THREE</description></item>
</channel></rss>
XML
my $items = app('parse_response', $rss);
is(scalar(@$items), 3, 'RSS skips entries without magnets');
is($items->[0]{title}, 'Café & Straße', 'XML entities and Unicode');
is($items->[0]{magnet}, 'magnet:?xt=urn:btih:ONE&dn=Cafe', 'enclosure magnet');
is($items->[1]{magnet}, 'magnet:?xt=urn:btih:TWO&dn=Second', 'CDATA magnet and HTML entities');
is($items->[2]{title}, '(untitled)', 'missing title');
is($items->[2]{index}, 3, 'indexes exclude skipped entries');
my $atom = app('parse_feed', '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom</title><updated>today</updated><link href="magnet:?xt=urn:btih:A"/></entry></feed>');
is($atom->[0]{published}, 'today', 'Atom namespaces and dates');
my $json = encode_json({torrents => [undef, {}, {title => 'JSON Show', magnet_url => 'magnet:?xt=urn:btih:J', date_released_unix => 1700000000}]});
my $json_items = app('parse_response', $json);
is($json_items->[0]{published}, '2023-11-14 22:13 UTC', 'JSON Unix timestamp uses UTC');
is_deeply(app('parse_eztv_response', '{}'), [], 'empty API response');
fails('malformed XML', qr/Could not parse feed XML/, sub { app('parse_feed', '<rss>') });
fails('malformed JSON', qr/Could not parse API response/, sub { app('parse_eztv_response', '{bad') });
fails('invalid JSON root', qr/not an object/, sub { app('parse_eztv_response', '[]') });
fails('invalid torrents type', qr/invalid 'torrents'/, sub { app('parse_eztv_response', '{"torrents":null}') });
fails('external XML entities rejected', qr/External XML entities/, sub { app('parse_feed', '<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///nonexistent">]><rss>&x;</rss>') });

ok(app('title_matches', 'Café Straße Episode', 'STRASSE café'), 'Unicode case folding and unordered words');
ok(!app('title_matches', 'First show', 'second'), 'nonmatching title');
is_deeply([map { $_->{index} } @{app('filter_items_by_title', $items, 'second')}], [2], 'filter preserves indexes');
is_deeply(app('parse_selections', '3-1, 2 2', $items), [3, 2, 1, 2, 2], 'descending ranges and repeated selections');
is_deeply(app('parse_selections', '1-3', $items), [1, 2, 3], 'ascending range');
is_deeply(app('parse_selections', ' ALL ', $items), [1, 2, 3], 'select all');
is_deeply(app('parse_selections', '', $items), [], 'blank selection');
fails('bad range', qr/Invalid range/, sub { app('parse_selections', '2-x', $items) });
fails('bad selection', qr/Invalid selection/, sub { app('parse_selections', 'x', $items) });
fails('unknown number', qr/Unknown item number\(s\): 9/, sub { app('select_items', $items, [9]) });

my $url = 'https://example.com/api/get-torrents?key=a%26b&empty=';
is(app('build_request_url', $url, 50, 1, undef), $url, 'custom URLs unchanged when no API parameters requested');
my %query = URI->new(app('build_request_url', $url, 7, 2, '00123'))->query_form;
is_deeply(\%query, {key => 'a&b', empty => '', limit => 7, page => 2, imdb_id => '00123'}, 'API URL preserves existing query values');
ok(app('is_eztv_api_url', $url), 'detect API URL');
ok(!app('is_eztv_api_url', 'https://example.com/feed.xml'), 'detect generic feed');

my $args = app('parse_arguments', '--search', 'Test', '--limit', '2', '--open', '1', '2', '--imdb-id', 'TT00123');
is_deeply($args->{open}, [1, 2], 'multiple --open numbers');
is($args->{imdb_id}, '00123', 'IMDb prefix and leading zeros');
is(app('parse_arguments', '-q', 'Test')->{title_search}, 'Test', 'legacy query alias');
for my $invalid (['--limit', 0], ['--page', 101], ['--probe-workers', 26], ['--series-results', 0], ['--timeout', 0], ['--limit', '1.5'], ['--imdb-id', 'abc'], ['--open'], ['--wat'], ['--title', 'x', '--query', 'y']) {
    fails("invalid arguments: @$invalid", qr/error:/, sub { app('parse_arguments', @$invalid) });
}

my $html = app('menu_html', [{index => 1, title => '<script>" & café', magnet => 'magnet:?xt=x&dn="test"', published => '<today>'}]);
like($html, qr/&lt;script&gt;&quot; &amp; café/, 'menu escapes title');
like($html, qr/href="magnet:\?xt=x&amp;dn=&quot;test&quot;"/, 'menu escapes links');
like($html, qr/>1 result<\/div>/, 'singular result count');
like(app('menu_html', []), qr/>0 results<\/div>/, 'empty menu count');
like($html, qr/li\[hidden\].*display: none/, 'CSS respects hidden search results');
unlike($html, qr/__MENU_/, 'all template slots filled');

{
    no warnings 'redefine';
    local *TorrentFeedBrowser::fetch_feed = sub {
        return encode_json([
            {show => {name => 'Series', externals => {imdb => 'tt0123'}, premiered => '2020-01-01', webChannel => {name => 'Web'}}},
            {show => {name => 'Duplicate', externals => {imdb => 'tt0123'}}},
            {show => {name => 'Missing ID'}},
            {show => {name => 'Other', externals => {imdb => 'TT0456'}}},
        ]);
    };
    my $matches = app('search_tvmaze', 'Test', 20);
    is(scalar(@$matches), 2, 'TVmaze IDs deduplicated and validated');
    is($matches->[0]{network}, 'Web', 'web channel fallback');
}
{
    no warnings 'redefine';
    local *TorrentFeedBrowser::search_tvmaze = sub { return [map { {imdb_id => "$_", name => "Series $_"} } 1 .. 6]; };
    local *TorrentFeedBrowser::fetch_feed = sub {
        my %params = URI->new($_[0])->query_form;
        select undef, undef, undef, (7 - $params{imdb_id}) * 0.005;
        return encode_json({torrents => $params{imdb_id} % 2 ? [{magnet_url => 'magnet:?xt=x'}] : []});
    };
    is_deeply([map { $_->{imdb_id} } @{app('find_indexed_series', $url, 'test', 20, 6, 3)}], [1, 3, 5], 'parallel probes preserve relevance order');
    is_deeply([map { $_->{imdb_id} } @{app('find_indexed_series', $url, 'test', 20, 2, 1)}], [1], 'serial probes and match limit');
    local *TorrentFeedBrowser::fetch_feed = sub { die "Could not fetch feed: test failure\n" };
    fails('worker errors propagated', qr/test failure/, sub { app('find_indexed_series', $url, 'test', 20, 6, 3) });
}

my $dir = tempdir(CLEANUP => 1);
my $feed = "$dir/feed.xml";
open my $fh, '>:encoding(UTF-8)', $feed or die $!;
print {$fh} $rss;
close $fh;
sub cli {
    my (@args) = @_;
    my $err = gensym;
    my $pid = open3(my $in, my $out, $err, $^X, "$FindBin::Bin/../torrent_feed_browser.pl", @args);
    close $in;
    local $/;
    my $stdout = <$out> // '';
    my $stderr = <$err> // '';
    waitpid $pid, 0;
    return ($? >> 8, $stdout, $stderr);
}
my ($status, $stdout, $stderr) = cli(URI::file->new_abs($feed)->as_string, '--no-interactive', '--no-color', '--save', "$dir/magnets.txt");
is($status, 0, 'CLI local feed and save exit successfully');
is($stderr, '', 'CLI has no warnings');
like($stdout, qr/Saved 3 magnet link/, 'CLI save confirmation');
open my $saved, '<:encoding(UTF-8)', "$dir/magnets.txt" or die $!;
my $saved_text = do { local $/; <$saved> };
close $saved;
like($saved_text, qr/Café & Straße\nmagnet:\?xt=urn:btih:ONE&dn=Cafe/, 'saved file is UTF-8 with complete links');
($status, $stdout, $stderr) = cli(URI::file->new_abs($feed)->as_string, '--search', 'second', '--no-interactive');
is($status, 0, 'CLI title filter succeeds');
is($stdout, "2. Second Show\n", 'CLI filtered listing');
($status, $stdout, $stderr) = cli(URI::file->new_abs($feed)->as_string, '--no-color');
is($status, 0, 'CLI interactive EOF exits cleanly');
like($stdout, qr/Download selection:/, 'CLI interactive prompt');
($status, $stdout, $stderr) = cli('--limit', '0');
is($status, 2, 'CLI invalid arguments return status 2');
($status, $stdout, $stderr) = cli('file:///nonexistent-torrent-test-feed', '--no-interactive');
is($status, 1, 'CLI fetch errors return status 1');
like($stderr, qr/Could not fetch feed/, 'CLI fetch error is readable');

{
    my $input = "invalid\n9\n3-2\n";
    my $output = '';
    open my $in, '<', \$input or die $!;
    open my $out, '>', \$output or die $!;
    local *STDIN = $in;
    local *STDOUT = $out;
    is_deeply([map { $_->{index} } @{app('prompt_for_items', $items, 0)}], [3, 2], 'interactive prompt retries invalid selections');
    like($output, qr/Invalid selection: invalid.*Unknown item number\(s\): 9/s, 'interactive validation messages');
}
{
    no warnings 'redefine';
    my $output = '';
    open my $out, '>', \$output or die $!;
    local *STDOUT = $out;
    local *TorrentFeedBrowser::search_tvmaze = sub { return [{name => 'Test Series', imdb_id => '123', premiered => '', network => ''}] };
    local *TorrentFeedBrowser::fetch_feed = sub { return $json };
    is(app('main', '--search', 'Test', '--no-interactive', '--no-color'), 0, 'full series-search workflow');
    like($output, qr/EZTV results for Test Series \(IMDb tt123\).*JSON Show/s, 'series-search output');
}

{
    no warnings 'redefine';
    my @opened;
    local *TorrentFeedBrowser::open_url = sub { push @opened, $_[0] };
    my $path = app('open_menu', $items);
    ok(-s $path, 'browser menu written');
    like($opened[0], qr/^file:/, 'browser opens a file URI');
    unlink $path;
    my $output = '';
    open my $capture, '>', \$output or die $!;
    local *STDOUT = $capture;
    app('open_magnets', [$items->[1]], 0);
    is($opened[1], $items->[1]{magnet}, 'magnet opening forwards exact URL');
}

done_testing;
