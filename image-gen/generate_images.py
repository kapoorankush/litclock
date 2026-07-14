#!/usr/bin/env python3
"""
Generate images for literary clock quotes.
Skips quotes that already have images.

NOTE: The PHP version (quote_to_image.php) produces better output and is
the preferred tool for generating quote images. Use this Python version
only as a fallback.
"""

import csv
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def clean_escape_sequences(text):
    """Clean up various levels of escaped quotes and newlines from text."""
    # Clean escaped newlines (various levels of escaping)
    text = text.replace("\\\\\\\\n", " ")  # \\\\n -> space
    text = text.replace("\\\\n", " ")  # \\n -> space
    text = text.replace("\\n", " ")  # \n -> space
    # Clean escaped quotes (various levels of escaping)
    text = text.replace('\\\\\\\\"', '"')  # \\\\" -> "
    text = text.replace('\\"', '"')  # \" -> "
    return text


# Configuration
SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent
CSV_FILE = SCRIPT_DIR / "litclock_annotated.csv"
IMAGE_DIR = PROJECT_DIR / "images"
METADATA_DIR = PROJECT_DIR / "images" / "metadata"

# Fonts
FONT_LIGHT = PROJECT_DIR / "fonts" / "Literata72pt-ExtraLight.ttf"
FONT_BOLD = PROJECT_DIR / "fonts" / "Literata72pt-Black.ttf"
FONT_CREDIT = PROJECT_DIR / "fonts" / "Literata72pt-SemiBoldItalic.ttf"

# Image dimensions
WIDTH = 800
HEIGHT = 400
MARGIN = 10


def measure_text(draw, text, font):
    """Measure the bounding box of text."""
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def fit_text(draw, quote_array, width, height, font_size, time_start, time_word_count, margin):
    """
    Recursively find optimal font size and render text.
    Returns (image, paragraph_height, font_size) or None if text doesn't fit.
    """
    try:
        font_light = ImageFont.truetype(str(FONT_LIGHT), font_size)
        font_bold = ImageFont.truetype(str(FONT_BOLD), font_size)
    except Exception as e:
        print(f"Error loading fonts: {e}")
        return None

    # Create fresh image for this attempt
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)

    position = [margin, margin + font_size]
    line_height = round(font_size * 1.618)

    for idx, word in enumerate(quote_array):
        # Highlight time words in bold
        if time_start <= idx <= time_start + time_word_count:
            font = font_bold
        else:
            font = font_light

        text_width, text_height = measure_text(draw, word + " ", font)

        # Word too wide for image
        if text_width > (width - margin):
            return None

        # Need to wrap to next line
        if (position[0] + text_width) >= (width - margin):
            position[0] = margin
            position[1] += line_height

        draw.text((position[0], position[1] - font_size), word, font=font, fill="black")
        position[0] += text_width

    paragraph_height = position[1]

    # If text fits with room for credits, try larger font
    if paragraph_height < height - 100:
        result = fit_text(draw, quote_array, width, height, font_size + 1, time_start, time_word_count, margin)
        if result is not None:
            return result

    # If paragraph is too tall, this size doesn't work
    if paragraph_height > height - 100:
        return None

    return (img, paragraph_height, font_size)


def add_credits(img, title, author, font_size=18):
    """Add title and author credits to the image."""
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(str(FONT_CREDIT), font_size)
    except Exception as e:
        print(f"Error loading credit font: {e}")
        return img

    dash = "\u2014"  # em-dash
    credits = f"{title}, {author}"
    full_text = dash + credits

    text_width, text_height = measure_text(draw, full_text, font)

    if text_width > 500:
        # Split credits into two lines
        words = credits.split(" ")
        best_split = None
        for i in range(1, len(words)):
            line1 = " ".join(words[: len(words) - i])
            line2 = " ".join(words[len(words) - i :])
            if len(line2) + 5 > len(line1):
                break
            best_split = (line1, line2)

        if best_split:
            w1, h1 = measure_text(draw, dash + best_split[0], font)
            w2, h2 = measure_text(draw, best_split[1], font)
            x1 = WIDTH - w1 - MARGIN
            x2 = WIDTH - w2 - MARGIN
            y = HEIGHT - MARGIN
            draw.text((x1, y - h1 * 2.1), dash + best_split[0], font=font, fill="black")
            draw.text((x2, y - h1), best_split[1], font=font, fill="black")
        else:
            x = WIDTH - text_width - MARGIN
            y = HEIGHT - MARGIN - text_height
            draw.text((x, y), full_text, font=font, fill="black")
    else:
        x = WIDTH - text_width - MARGIN
        y = HEIGHT - MARGIN - text_height
        draw.text((x, y), full_text, font=font, fill="black")

    return img


def generate_quote_image(time_key, quote, timestring, title, author, image_num, is_nsfw=False):
    """Generate images for a single quote."""
    # Find timestring position in quote
    quote_lower = quote.lower()
    timestring_lower = timestring.lower()
    pos = quote_lower.find(timestring_lower)

    if pos == -1:
        return False

    # Count words before timestring
    before_text = quote[:pos]
    time_start = len(before_text.split())
    time_word_count = len(timestring.split()) - 1

    quote_array = quote.split()

    # Create a temporary draw context for fitting
    temp_img = Image.new("RGB", (WIDTH, HEIGHT), "white")
    temp_draw = ImageDraw.Draw(temp_img)

    # Find optimal font size and create image
    result = fit_text(temp_draw, quote_array, WIDTH, HEIGHT, 18, time_start, time_word_count, MARGIN)

    if result is None:
        return False

    img, paragraph_height, font_size = result

    # Save quote image (without credits)
    nsfw_suffix = "_nsfw" if is_nsfw else ""
    quote_path = IMAGE_DIR / f"quote_{time_key}_{image_num}{nsfw_suffix}.png"
    img.save(quote_path)

    # Add credits and save metadata version
    img_with_credits = add_credits(img.copy(), title, author)
    metadata_path = METADATA_DIR / f"quote_{time_key}_{image_num}{nsfw_suffix}_credits.png"
    img_with_credits.save(metadata_path)

    return True


def main():
    """Main execution."""
    print("=" * 60)
    print("Literary Clock Image Generator")
    print("=" * 60)

    # Ensure directories exist
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    METADATA_DIR.mkdir(parents=True, exist_ok=True)

    # Track image numbers per time
    image_numbers = {}
    generated = 0
    skipped = 0
    errors = 0
    total = 0
    missing_nsfw_field = 0

    with open(CSV_FILE, encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f, delimiter="|")
        for row in reader:
            if len(row) < 5:
                continue

            total += 1
            time_str = row[0]
            timestring = clean_escape_sequences(row[1].strip())
            quote = clean_escape_sequences(row[2])
            quote = " ".join(quote.split())  # Normalize whitespace
            title = row[3].strip()
            author = row[4].strip()

            # Check for IS_NSFW field
            if len(row) <= 5 or not row[5].strip():
                missing_nsfw_field += 1
                is_nsfw = False  # Default to SFW if not specified
            else:
                is_nsfw = row[5].strip().upper() == "YES"

            # Convert time to HHMM format
            time_key = time_str[:2] + time_str[3:5]

            # Determine image number
            if time_key not in image_numbers:
                image_numbers[time_key] = 0
            image_num = image_numbers[time_key]
            image_numbers[time_key] += 1

            # Check if images already exist
            nsfw_suffix = "_nsfw" if is_nsfw else ""
            quote_path = IMAGE_DIR / f"quote_{time_key}_{image_num}{nsfw_suffix}.png"
            metadata_path = METADATA_DIR / f"quote_{time_key}_{image_num}{nsfw_suffix}_credits.png"

            if quote_path.exists() and metadata_path.exists():
                skipped += 1
                continue

            # Generate the image
            success = generate_quote_image(time_key, quote, timestring, title, author, image_num, is_nsfw)
            if success:
                generated += 1
                if generated % 100 == 0:
                    print(f"Generated {generated} images...")
            else:
                errors += 1
                if errors < 10:
                    print(f"Warning: Failed to generate image for {time_key}_{image_num}")

    print()
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"Total quotes: {total}")
    print(f"Skipped (already exist): {skipped}")
    print(f"Generated new: {generated}")
    print(f"Errors: {errors}")

    if missing_nsfw_field > 0:
        print()
        print(f"WARNING: {missing_nsfw_field} quotes missing IS_NSFW field!")
        print("Run NSFW detection to review these quotes:")
        print("  python detect_nsfw.py --keywords-only")
        print("  python review_nsfw.py interactive")
        print("  python review_nsfw.py merge")


if __name__ == "__main__":
    main()
