"""
Entry point for the PhoneBox Digital Twin simulator.

Run with:
    python -m phonebox_simulator.main
"""

import sys

from PySide6.QtWidgets import QApplication

from phonebox_simulator.ui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
