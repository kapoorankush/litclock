<?php

// this script turns quotes from books into images for use in a Kindle clock.
// Jaap Meijers, 2018


error_reporting(E_ALL);
ini_set("display_errors", 1);

$imagenumber = 0;
$previoustime = 0;

// pad naar font file
putenv('GDFONTPATH=' . realpath(dirname(__FILE__) . '/../fonts'));
$font_path = "Literata72pt-ExtraLight.ttf";
$font_path_bold = "Literata72pt-Black.ttf";
$creditFont = "Literata72pt-SemiBoldItalic.ttf";


// get the quotes (including title and author) from a CSV file,
// and create unique images for them, one without and one with title and author
//
// Counters are dispatched from TurnQuoteIntoImage's return status (#216):
//   'written'         — both quote PNG and credits PNG saved successfully
//   'failed_nostring' — stristr could not locate the timestring in the quote
//   'failed_nofit'    — fitText exhausted the font range
//   'failed_write'    — either imagepng() call returned false (disk/perm/etc.)
//
// Skip rule (#299): an existing PNG is reused only if the manifest's recorded
// content hash for its filename matches the hash of the current row. Otherwise
// the row is regenerated. Pre-#299 behaviour was a bare file_exists() short-
// circuit, which silently kept stale PNGs after CSV row reorders.
$manifest_path = '../images/manifest.json';
$existing_manifest_files = [];
if (file_exists($manifest_path)) {
    $raw_manifest = @file_get_contents($manifest_path);
    if ($raw_manifest !== false) {
        $decoded = json_decode($raw_manifest, true);
        if (is_array($decoded) && isset($decoded['files']) && is_array($decoded['files'])) {
            $existing_manifest_files = $decoded['files'];
        }
    }
}
$new_manifest_files = [];

$row = 1;
$skipped = 0;
$written = 0;
$failed_nostring = 0;
$failed_nofit = 0;
$failed_write = 0;
if (($handle = fopen("litclock_annotated.csv", "r")) !== FALSE) {
    while (($data = fgetcsv($handle, 5000, "|")) !== FALSE) {
        $num = count($data);
        if ($num < 5) continue;
        $row++;
        $time = $data[0];
        $timestring = trim($data[1]);
        // Hash inputs are the trimmed CSV values, BEFORE the escape-sequence
        // cleanup below — that way Python (corpus_edit.py) can compute the same
        // hash from the same CSV without replicating PHP's str_replace chain.
        $quote_for_hash = trim($data[2]);
        $quote = $data[2];
        // Clean up escape sequences (various levels of escaped quotes and newlines)
        $quote = str_replace('\\\\\\\\n', ' ', $quote);  // \\\\n -> space
        $quote = str_replace('\\\\n', ' ', $quote);      // \\n -> space
        $quote = str_replace('\\n', ' ', $quote);        // \n -> space
        $quote = str_replace('\\\\\\\\\\"', '"', $quote); // \\\\" -> "
        $quote = str_replace('\\\\\\"', '"', $quote);    // \\" -> "
        $quote = str_replace('\\"', '"', $quote);        // \" -> "
        $quote = trim(preg_replace('/\s+/', ' ', $quote));
        $title = trim($data[3]);
        $author = trim($data[4]);
        $is_nsfw = (count($data) > 5 && strtoupper(trim($data[5])) == 'YES');
        $nsfw_suffix = $is_nsfw ? '_nsfw' : '';

        // Determine image number before generating
        $timeKey = substr($time, 0, 2).substr($time, 3, 2);
        if ($timeKey == $previoustime) {
            $imagenumber++;
        } else {
            $imagenumber = 0;
        }
        $previoustime = $timeKey;

        $image_filename = 'quote_'.$timeKey.'_'.$imagenumber.$nsfw_suffix.'.png';
        $metadata_filename = 'quote_'.$timeKey.'_'.$imagenumber.$nsfw_suffix.'_credits.png';
        $imagePath = '../images/'.$image_filename;
        $metadataPath = '../images/metadata/'.$metadata_filename;
        // #299/F: JSON-encoded array preimage. Pipe-joining was ambiguous when
        // any field legally contained a `|` (CSV-quoted fields can). The flags
        // pin Python parity: JSON_UNESCAPED_SLASHES + JSON_UNESCAPED_UNICODE
        // mirror Python's `ensure_ascii=False` and default slash treatment.
        $content_hash = sha1(json_encode(
            [$quote_for_hash, $title, $author, $timestring],
            JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE
        ));

        // Skip only when both PNGs exist AND the manifest's recorded hash for
        // this filename matches. Manifest miss / hash mismatch = regenerate.
        $existing_hash = isset($existing_manifest_files[$image_filename])
            ? $existing_manifest_files[$image_filename]
            : null;
        $hash_match = ($existing_hash !== null && $existing_hash === $content_hash);
        if (file_exists($imagePath) && file_exists($metadataPath) && $hash_match) {
            $skipped++;
            $new_manifest_files[$image_filename] = $content_hash;
            continue;
        }

        $status = TurnQuoteIntoImage($time, $quote, $timestring, $title, $author, $imagenumber, $nsfw_suffix);
        switch ($status) {
            case 'written':         $written++; $new_manifest_files[$image_filename] = $content_hash; break;
            case 'failed_nostring': $failed_nostring++; break;
            case 'failed_nofit':    $failed_nofit++; break;
            case 'failed_write':    $failed_write++; break;
        }
        if ($written > 0 && $written % 100 == 0) {
            print "Written $written images so far...\n";
        }
    }
    fclose($handle);
}

// Write the manifest sidecar (#299). corpus_hash + generator_hash let the CI
// gate validate that the release was produced from the PR's CSV; the per-file
// hash map drives this script's hash-based skip on next run.
$manifest = [
    'corpus_hash' => sha1_file('litclock_annotated.csv'),
    'generator_hash' => sha1_file(__FILE__),
    'created_at' => gmdate('Y-m-d\TH:i:s\Z'),
    'files' => $new_manifest_files,
];
$manifest_write_failed = false;
$manifest_json = json_encode($manifest, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES);
if ($manifest_json === false) {
    print "ERROR: failed to encode manifest.json — manifest NOT written\n";
    $manifest_write_failed = true;
} elseif (file_put_contents($manifest_path, $manifest_json."\n") === false) {
    print "ERROR: failed to write manifest.json at $manifest_path\n";
    $manifest_write_failed = true;
} else {
    print "Wrote manifest.json with ".count($new_manifest_files)." entries (corpus_hash=".substr($manifest['corpus_hash'], 0, 12)."...)\n";
}
$failed = $failed_nostring + $failed_nofit + $failed_write;
$present_on_disk = count(glob('../images/quote_*.png'));
$expected_from_csv = $row - 1;  // $row starts at 1, increments once per processed data row
$gap = $expected_from_csv - $present_on_disk;

print "\n=== Summary ===\n";
printf("Total rows:              %d\n", $row);
printf("Skipped (already exist): %d\n", $skipped);
printf("Written:                 %d\n", $written);
printf("Failed:                  %d\n", $failed);
if ($failed > 0) {
    printf("  |- timestring not found: %d\n", $failed_nostring);
    printf("  |- could not fit text:   %d\n", $failed_nofit);
    printf("  `- imagepng write error: %d\n", $failed_write);
}
printf("Images on disk:          %d\n", $present_on_disk);
printf("Gap (CSV rows w/o img):  %d\n", $gap);

// #299: exit non-zero on any row-render failure or manifest write failure.
// Previously the script always exited 0, letting `corpus_edit ship` proceed
// past partial generations and ship a release whose corpus_hash matches the
// CSV but whose tarball is missing PNGs for failed rows.
if ($failed > 0 || $manifest_write_failed) {
    fwrite(STDERR, "\nERROR: generator did not complete cleanly (failed=$failed, manifest_write_failed=".($manifest_write_failed?'yes':'no').") — refusing to exit 0.\n");
    exit(1);
}



function TurnQuoteIntoImage($time, $quote, $timestring, $title, $author, $imagenumber, $nsfw_suffix = '') {

    global $font_path;
    global $font_path_bold;
    global $creditFont;

    //image dimensions
    $width = 800;
    $height = 400;

    //text margin
    $margin = 10;

    // first, find the timestring to be highlighted in the quote.
    // We bold the EXACT character span of the timestring, not whole space-
    // delimited words: a timestring joined to the prior word by a hyphen/dash
    // ("...the right moment-four minutes past ten...") would otherwise drag the
    // leading fragment ("moment-") into bold. `$before` is everything up to the
    // (case-insensitive) match, so its byte length is the timestring's start
    // offset in the (single-space-normalised) quote.
    $before = stristr($quote, $timestring, true);
    if ($before === false) {
        global $row;
        printf("Warning: timestring not found in quote for %s (idx=%d, img=%d, timestring=%s) — %.80s...\n",
            $time, $row - 1, $imagenumber, $timestring, $quote);
        return 'failed_nostring';
    }
    // UTF-8 note: these are BYTE offsets, but they are always aligned to real
    // character boundaries — $before is the literal (case-insensitive) match
    // prefix, so $ts_char_start / $ts_char_end can never land inside a multibyte
    // sequence. The per-word split below (substr) is therefore never able to cut
    // a multibyte glyph in half, even for accented/em-dash quotes.
    $ts_char_start = strlen($before);
    $ts_char_end   = $ts_char_start + strlen($timestring);

    // Extend the bold end past characters attached to the last time-word, but
    // do NOT drag a hyphen/dash-joined FOLLOWING word into bold. Three cases:
    //   1. Timestring ends MID-WORD ("ten" inside "tenth"): the next char is a
    //      letter/number — keep the whole word bold exactly as the original
    //      whole-word bolder did. A genuinely wrong mid-word match is a corpus
    //      data issue (see #502), not a renderer concern; we don't want to turn
    //      it into a NEW mid-word bold here.
    //   2. Timestring ends at a word boundary with TERMINATING punctuation
    //      ("midnight." / "...past ten,"): the punctuation runs to the next
    //      space (or end) with no letter/number after it — keep it bold, matching
    //      the original bolder and avoiding churn on ~2400 images.
    //   3. Timestring ends at a hyphen/dash-join ("...four minutes to ten-four...",
    //      #504): the punctuation run hits a letter/number before any space —
    //      bold only the timestring ("ten"), symmetric to the leading-fragment
    //      fix in #503.
    //
    // "letter/number" is UTF-8-aware (\p{L}\p{N} over the char starting at the
    // byte offset), so a join into a NON-ASCII word ("ten—東京", "ten-δεκα") is
    // caught too, while multibyte punctuation (em-dash, ellipsis, curly quotes —
    // all \p{P}) is treated as punctuation, same as ASCII (#504 review). Offsets
    // stay at char boundaries: ts_char_end is the match end, and the scan advances
    // whole UTF-8 chars.
    $qlen = strlen($quote);
    $word_char_at = function ($i) use ($quote, $qlen) {
        // Is a Unicode letter or number the char starting at byte $i? (4-byte
        // window always spans the full first char; the anchored match ignores
        // any trailing partial bytes of the next char.)
        return $i < $qlen && preg_match('/^[\p{L}\p{N}]/u', substr($quote, $i, 4)) === 1;
    };
    if ($word_char_at($ts_char_end)) {
        // case 1 — mid-word: keep the whole word bold
        while ($ts_char_end < $qlen && $quote[$ts_char_end] !== ' ') {
            $ts_char_end++;
        }
    } else {
        // cases 2 & 3 — look ahead over the trailing punctuation run: extend only
        // if it terminates the word (reaches a space/end before any letter/number).
        $scan = $ts_char_end;
        while ($scan < $qlen && $quote[$scan] !== ' ' && !$word_char_at($scan)) {
            $b = ord($quote[$scan]);           // advance one whole UTF-8 char
            $scan += ($b < 0x80) ? 1 : (($b < 0xE0) ? 2 : (($b < 0xF0) ? 3 : 4));
        }
        if ($scan >= $qlen || $quote[$scan] === ' ') {
            $ts_char_end = $scan;
        }
    }

    // divide text in an array of words, based on spaces
    $quote_array = explode(' ', $quote);

    $time = substr($time, 0, 2).substr($time, 3, 2);

    // font size to start with looking for a fit. a long quote of 125 words or 700 characters gives us a font size of 23, so 18 is a safe start.
    $font_size = 18;

    ///// QUOTE /////
    // find the font size (recursively) for an optimal fit of the text in the bounding box
    // and create the image.
    $result = fitText($quote_array, $width, $height, $font_size, $ts_char_start, $ts_char_end, $margin);
    if ($result === false) {
        global $row;
        printf("Warning: Could not fit text for %s (idx=%d, img=%d) — %.80s...\n",
            $time, $row - 1, $imagenumber, $quote);
        return 'failed_nofit';
    }
    list($png_image) = $result;

    print "Image for " . $time .'_'. $imagenumber . $nsfw_suffix . "\n";

    // Save the image
    $quotePath = '../images/quote_'.$time.'_'.$imagenumber.$nsfw_suffix.'.png';
    if (imagepng($png_image, $quotePath) === false) {
        print "Error: imagepng write failed for $quotePath\n";
        imagedestroy($png_image);
        return 'failed_write';
    }


    ///// METADATA /////
    // create another version, with title and author in the image

    
    // define text color
    $grey = imagecolorallocate($png_image, 0, 0, 0);
    $black = imagecolorallocate($png_image, 0, 0, 0);

    $dash = "—";

    $credits = $title . ", " . $author;
    $creditFont_size = 18;

    // if the metadata are longer than 45 characters, replace a space by a newline from the end,
    // just as long the paragraph is getting smaller. stop when the box gets wider again.
    list($metawidth, $metaheight, $metaleft, $metatop) = measureSizeOfTextbox($creditFont_size, $creditFont, $dash . $credits);
    
    if ( $metawidth > 500 ) {

        $newCredits = array();

        $creditsArray = explode(" ", $credits);
        
        $i = 1;

        while ( True ) {

            // cut the metadata in two lines
            $tmp0 = implode(" ", array_slice($creditsArray, 0, count($creditsArray)-$i));
            $tmp1 = implode(" ", array_slice($creditsArray, 0-$i));

            // once the second line is (almost) longer than the first line, stop
            if ( strlen($tmp1)+5 > strlen($tmp0) ) {
                break;
            } else { 
                // if the second line is still shorter than the first, save it to a new string, but continue to look at a new fit.
                $newCredits[0] = $tmp0;
                $newCredits[1] = $tmp1;
            }

            $i++;

        }

        list($textWidth1, $textheight1) = measureSizeOfTextbox($creditFont_size, $creditFont, $dash . $newCredits[0]);
        list($textWidth2, $textheight2) = measureSizeOfTextbox($creditFont_size, $creditFont, $newCredits[1]);

        $metadataX1 = $width-($textWidth1+$margin);
        $metadataX2 = $width-($textWidth2+$margin);
        $metadataY = $height-$margin;

        imagettftext($png_image, $creditFont_size, 0, $metadataX1, $metadataY-($textheight1*1.1), $black, $creditFont, $dash . $newCredits[0]);
        imagettftext($png_image, $creditFont_size, 0, $metadataX2, $metadataY, $black, $creditFont, $newCredits[1]);
        
    } else {

        // position of single line metadata
        $metadataX = ($width-$metaleft)-$margin;
        $metadataY = $height-$margin;

        imagettftext($png_image, $creditFont_size, 0, $metadataX, $metadataY, $black, $creditFont, $dash . $credits);

    }

    // Save the image with metadata. If this fails after the quote PNG succeeded,
    // we leave the orphan quote PNG on disk and skip writing the manifest entry
    // (the success branch in the main loop only records `written`). On next run
    // the manifest miss + missing credits PNG combine to retry both. This is
    // the #299 update of the original #216 D2 self-heal: previously the
    // bare file_exists check caught the mismatch, now the manifest does.
    $creditsPath = '../images/metadata/quote_'.$time.'_'.$imagenumber.$nsfw_suffix.'_credits.png';
    if (imagepng($png_image, $creditsPath) === false) {
        print "Error: imagepng write failed for $creditsPath\n";
        imagedestroy($png_image);
        return 'failed_write';
    }

    // Free up memory
    imagedestroy($png_image);

    return 'written';

    // convert the image we made to greyscale
    //$im = new Imagick();
    //$im->readImage('images/quote_'.$time.'_'.$imagenumber.'.png');
    //$im->setImageType(Imagick::IMGTYPE_GRAYSCALE);
    //unlink('images/quote_'.$time.'_'.$imagenumber.'.png');
    //$im->writeImage('images/quote_'.$time.'_'.$imagenumber.'.png');

    // convert the image we made to greyscale 
    //$im = new Imagick();
    //$im->readImage('images/metadata/quote_'.$time.'_'.$imagenumber.'_credits.png');
    //$im->setImageType(Imagick::IMGTYPE_GRAYSCALE);
    //unlink('images/metadata/quote_'.$time.'_'.$imagenumber.'_credits.png');
    //$im->writeImage('images/metadata/quote_'.$time.'_'.$imagenumber.'_credits.png');

}


function fitText($quote_array, $width, $height, $font_size, $ts_char_start, $ts_char_end, $margin) {

    global $font_path_bold;
    global $font_path;

    // create image. NOTE: PHP CLI's `die("string")` exits 0 (only `die(int)`
    // sets a non-zero status), which would let corpus_edit ship treat a fatal
    // GD failure as success. Use explicit exit(1) instead. (#299)
    $png_image = imagecreate($width, $height);
    if ($png_image === false) {
        fwrite(STDERR, "Cannot Initialize new GD image stream\n");
        exit(1);
    }
    $background_color = imagecolorallocate($png_image, 255, 255, 255);

    // define text color
    $grey = imagecolorallocate($png_image, 0, 0, 0);
    $black = imagecolorallocate($png_image, 0, 0, 0);

    $timeLocation = 0;
    $lineWidth = 0;

    // variable to hold the x and y position of words
    $position = array($margin,$margin+$font_size);

    // byte offset of the current word's first char in the single-space-joined
    // quote — used to intersect each word with the timestring char span so we
    // bold the exact characters, not whole words.
    $char_offset = 0;

    foreach($quote_array as $key => $word) {

        $wlen = strlen($word);
        // bold sub-range within this word, in local (0..$wlen) coordinates
        $b0 = max($ts_char_start, $char_offset) - $char_offset;
        $b1 = min($ts_char_end, $char_offset + $wlen) - $char_offset;
        $has_bold   = ($b1 > $b0 && $b0 < $wlen && $b1 > 0);
        $fully_bold = ($has_bold && $b0 <= 0 && $b1 >= $wlen);

        if ( !$has_bold || $fully_bold ) {

            // Non-boundary word (entirely regular OR entirely bold): render via
            // the ORIGINAL single-font path — same font AND same palette-index
            // colour ($grey for regular, $black for bold, both RGB 0,0,0) — so
            // word-aligned timestrings (the vast majority) stay byte-for-byte
            // identical to before.
            $font      = $fully_bold ? $font_path_bold : $font_path;
            $textcolor = $fully_bold ? $black          : $grey;

            // measure the word's width
            list($textwidth, $textheight) = measureSizeOfTextbox($font_size, $font, $word . " ");

            // if one word exceeds the width of the image, stop enlarging the font.
            if ( $textwidth > ($width - $margin) ) {
                return False;
            }

            // wrap to the next line if the word overflows the current one.
            if ( ($position[0] + $textwidth) >= ($width - $margin) ) {
                $position[0] = $margin;
                $position[1] = $position[1] + round($font_size*1.618); // 'golden ratio' line height
            }
            imagettftext($png_image, $font_size, 0, $position[0], $position[1], $textcolor, $font, $word);
            $position[0] += $textwidth;

        } else {

            // Boundary word: the timestring starts and/or ends inside this word
            // (e.g. "moment-four"). Split into regular-prefix / bold / regular-
            // suffix segments and draw them adjacent (no space between). The
            // per-segment widths use the SAME measureSizeOfTextbox metric as
            // above, so measurement and drawing stay consistent.
            $b0 = max(0, $b0); $b1 = min($wlen, $b1);
            // each segment: array(text, font, colour) — regular uses $grey, the
            // bold middle uses $black, matching the non-boundary palette indices.
            $segments = array();
            if ( $b0 > 0 )     { $segments[] = array(substr($word, 0, $b0), $font_path, $grey); }
            $segments[]        = array(substr($word, $b0, $b1 - $b0), $font_path_bold, $black);
            if ( $b1 < $wlen ) { $segments[] = array(substr($word, $b1), $font_path, $grey); }

            // total advance width = sum of segment widths + a trailing space
            // (measured in the regular font, matching inter-word spacing).
            $textwidth = 0;
            foreach ( $segments as $seg ) {
                list($sw) = measureSizeOfTextbox($font_size, $seg[1], $seg[0]);
                $textwidth += $sw;
            }
            list($spacewidth) = measureSizeOfTextbox($font_size, $font_path, " ");
            $textwidth += $spacewidth;

            if ( $textwidth > ($width - $margin) ) {
                return False;
            }
            if ( ($position[0] + $textwidth) >= ($width - $margin) ) {
                $position[0] = $margin;
                $position[1] = $position[1] + round($font_size*1.618);
            }

            // draw each segment at the running x, advancing by its own width.
            $x = $position[0];
            foreach ( $segments as $seg ) {
                imagettftext($png_image, $font_size, 0, $x, $position[1], $seg[2], $seg[1], $seg[0]);
                list($sw) = measureSizeOfTextbox($font_size, $seg[1], $seg[0]);
                $x += $sw;
            }
            $position[0] += $textwidth;

        }

        // advance the char cursor past this word + its single-space separator.
        $char_offset += $wlen + 1;

    }

    // if the height of the whole text is smaller than the height of the image, then call this same function again
    $paragraphHeight = $position[1];
    if ( $paragraphHeight < $height-100 ) { // leaving room for the credits below
        $result = fitText($quote_array, $width, $height, $font_size+1, $ts_char_start, $ts_char_end, $margin);
        if ( $result !== False ) {
            list($png_image, $paragraphHeight, $font_size, $timeLocation) = $result;
        };
    } else {
        // if this call to fitText returned a paragraph that is in fact higher than the height of the image,
        // then return without those values
        return False;
    }

    return array($png_image, $paragraphHeight, $font_size, $timeLocation);

}

function measureSizeOfTextbox($font_size, $font_path, $text) {

    $box = imagettfbbox($font_size, 0, $font_path, $text);

    $min_x = min( array($box[0], $box[2], $box[4], $box[6]) );
    $max_x = max( array($box[0], $box[2], $box[4], $box[6]) );
    $min_y = min( array($box[1], $box[3], $box[5], $box[7]) );
    $max_y = max( array($box[1], $box[3], $box[5], $box[7]) );

    $width  = ( $max_x - $min_x );
    $height = ( $max_y - $min_y );
    $left   = abs( $min_x ) + $width;
    $top    = abs( $min_y ) + $height;

    return array($width, $height, $left, $top);

}


?>
