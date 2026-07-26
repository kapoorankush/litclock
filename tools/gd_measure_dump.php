<?php
// GD brect oracle for tools/validate_measurement.py `generate` (dev box only —
// the Pi ships no PHP; it checks against the committed dump this produces).
// Extends the spike's gd_width_dump.php: full production font-size range
// (18..110, not 18..44) and all four brect corners (vertical metrics gate
// gd_bbox, which places the credits block).
// Input: word-list file (one string per line, trailing spaces significant).
// Output: font_tag \t size \t string_index \t xmin \t xmax \t ymin \t ymax
$home = getenv('LITCLOCK_HOME') ?: dirname(__DIR__);
putenv('GDFONTPATH=' . $home . '/fonts');
$fonts = [
    'REG' => 'Literata72pt-ExtraLight.ttf',
    'BOLD' => 'Literata72pt-Black.ttf',
    'CRED' => 'Literata72pt-SemiBoldItalic.ttf',
];
if ($argc < 2) { fwrite(STDERR, "usage: php gd_measure_dump.php <wordlist>\n"); exit(1); }
$strings = [];
foreach (file($argv[1]) as $l) {
    $strings[] = rtrim($l, "\n"); // preserve trailing spaces
}
printf("#gd|%s|php|%s\n", gd_info()['GD Version'], PHP_VERSION);
foreach ($fonts as $tag => $path) {
    for ($fs = 18; $fs <= 110; $fs++) {
        foreach ($strings as $i => $s) {
            $box = imagettfbbox($fs, 0, $path, $s);
            $xmin = min($box[0],$box[2],$box[4],$box[6]);
            $xmax = max($box[0],$box[2],$box[4],$box[6]);
            $ymin = min($box[1],$box[3],$box[5],$box[7]);
            $ymax = max($box[1],$box[3],$box[5],$box[7]);
            printf("%s\t%d\t%d\t%d\t%d\t%d\t%d\n", $tag, $fs, $i, $xmin, $xmax, $ymin, $ymax);
        }
    }
}
