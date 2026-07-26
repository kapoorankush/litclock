import display_driver


def main():
    try:
        epd = display_driver.get_panel()
        epd.init()
        epd.Clear()
        epd.sleep()
    except OSError as e:
        print(e)


if __name__ == "__main__":
    main()
