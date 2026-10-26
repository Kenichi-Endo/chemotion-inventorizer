"""Application entry point for launchers and installed console scripts."""
from chemotion_inventorizer.gui import Inventorizer


def main():
    Inventorizer().mainloop()


if __name__ == "__main__":
    main()
