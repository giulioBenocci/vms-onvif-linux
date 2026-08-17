"""
qtcompat - astrae la differenza fra PyQt6 e PySide6.

Perche' serve: Ubuntu 24.04 (e quindi Linux Mint 22) impacchetta PyQt6 ma
non PySide6. Un .deb che dipenda da PySide6 sarebbe costretto a includere
la wheel PyPI (~150 MB) invece di usare i pacchetti della distribuzione.
Chi installa da pip, pero', tipicamente si ritrova PySide6: supportiamo
entrambi.

Le API Qt6 dei due binding coincidono quasi ovunque. Le differenze che
contano qui sono due:

  * i nomi dei segnali (pyqtSignal / Signal);
  * gli enum, che in PyQt6 devono essere qualificati per intero
    (Qt.WidgetAttribute.WA_NativeWindow). PySide6 accetta anche la forma
    lunga, quindi nel resto del codice usiamo sempre quella.
"""

from __future__ import annotations

QT_API = ""

try:
    from PyQt6.QtCore import (  # noqa: F401
        QObject,
        Qt,
        QThread,
        QTimer,
        pyqtSignal as Signal,
        pyqtSlot as Slot,
    )
    from PyQt6.QtGui import QAction, QIcon, QKeySequence, QShortcut  # noqa: F401
    from PyQt6.QtWidgets import (  # noqa: F401
        QApplication,
        QCheckBox,
        QDialog,
        QDialogButtonBox,
        QDoubleSpinBox,
        QFormLayout,
        QFrame,
        QGridLayout,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QStatusBar,
        QVBoxLayout,
        QWidget,
    )

    QT_API = "PyQt6"

except ImportError:  # pragma: no cover - dipende dall'ambiente
    try:
        from PySide6.QtCore import (  # noqa: F401
            QObject,
            Qt,
            QThread,
            QTimer,
            Signal,
            Slot,
        )
        from PySide6.QtGui import QAction, QIcon, QKeySequence, QShortcut  # noqa: F401
        from PySide6.QtWidgets import (  # noqa: F401
            QApplication,
            QCheckBox,
            QDialog,
            QDialogButtonBox,
            QDoubleSpinBox,
            QFormLayout,
            QFrame,
            QGridLayout,
            QLabel,
            QLineEdit,
            QMainWindow,
            QMessageBox,
            QStatusBar,
            QVBoxLayout,
            QWidget,
        )

        QT_API = "PySide6"

    except ImportError as exc:
        raise SystemExit(
            "Serve un binding Qt6 per Python.\n"
            "  Debian/Ubuntu/Mint : sudo apt install python3-pyqt6\n"
            "  pip                : pip install PySide6\n"
            f"Dettaglio: {exc}"
        ) from exc


__all__ = [
    "QT_API",
    "QObject", "Qt", "QThread", "QTimer", "Signal", "Slot",
    "QAction", "QIcon", "QKeySequence", "QShortcut",
    "QApplication", "QCheckBox", "QDialog", "QDialogButtonBox",
    "QDoubleSpinBox", "QFormLayout", "QFrame", "QGridLayout", "QLabel",
    "QLineEdit", "QMainWindow", "QMessageBox", "QStatusBar", "QVBoxLayout",
    "QWidget",
]
