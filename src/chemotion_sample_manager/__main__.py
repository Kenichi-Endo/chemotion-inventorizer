"""Run Chemotion Sample Manager with ``python -m chemotion_sample_manager``."""


def main():
    from chemotion_sample_manager.gui import SampleManager

    SampleManager().mainloop()


if __name__ == "__main__":
    main()
