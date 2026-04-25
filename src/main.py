import sys
import traceback
from PySide6.QtWidgets import QApplication
# Make sure src is in path if not already
sys.path.append('src')
try:
    from gui.main_window import MainWindow
except ImportError:
    # Fallback if imported from root
    from src.gui.main_window import MainWindow

def main():
    try:
        app = QApplication(sys.argv)
        window = MainWindow()
        window.show()
        sys.exit(app.exec())
    except Exception:
        traceback.print_exc()
        input("Press Enter to close...")

if __name__ == "__main__":
    main()
