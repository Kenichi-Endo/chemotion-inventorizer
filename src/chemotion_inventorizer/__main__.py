"""Run Chemotion Inventorizer with ``python -m chemotion_inventorizer``."""


def main():
    from chemotion_inventorizer.gui import Inventorizer

    Inventorizer().mainloop()


if __name__ == "__main__":
    main()
