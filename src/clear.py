from display_driver import epd7in5


def main():
    try:
        epd = epd7in5.EPD()
        epd.init()
        epd.Clear()
        epd.sleep()
    except OSError as e:
        print(e)


if __name__ == "__main__":
    main()
