import sys

from PySide6.QtWidgets import QApplication

from app.main_window import MainWindow
from app.theme import STYLESHEET


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Secure Cloud Sync Terminal")
    app.setStyleSheet(STYLESHEET)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
