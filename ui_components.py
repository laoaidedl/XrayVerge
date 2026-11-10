import math
from collections import deque
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame, QSizePolicy
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QPointF
from PyQt5.QtGui import QPainter, QColor, QPen, QBrush, QPainterPath, QFont

def format_speed(speed_bps: float) -> str:
    if speed_bps < 1024:
        return f"{speed_bps:.2f} B/s"
    elif speed_bps < 1024 * 1024:
        return f"{speed_bps / 1024:.2f} KB/s"
    else:
        return f"{speed_bps / (1024 * 1024):.2f} MB/s"

class SpeedWaveWidget(QWidget):
    """Bottom-left real-time traffic wave chart matching Clash Verge"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(120)
        self.up_history = deque([0.0] * 35, maxlen=35)
        self.down_history = deque([0.0] * 35, maxlen=35)
        self.cur_up = 0.0
        self.cur_down = 0.0

        # Sub-labels
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 10)
        layout.addStretch()

        self.up_label = QLabel("↑  0.00 B/s")
        self.up_label.setStyleSheet("color: #f97316; font-size: 13px; font-weight: bold; background: transparent;")
        layout.addWidget(self.up_label)

        self.down_label = QLabel("↓  0.00 B/s")
        self.down_label.setStyleSheet("color: #0ea5e9; font-size: 13px; font-weight: bold; background: transparent;")
        layout.addWidget(self.down_label)

        self.status_label = QLabel("⚙  Xray-core 准备就绪")
        self.status_label.setStyleSheet("color: #64748b; font-size: 11px; background: transparent;")
        layout.addWidget(self.status_label)

    def add_data(self, up_speed: float, down_speed: float, status_text: str = ""):
        self.cur_up = up_speed
        self.cur_down = down_speed
        self.up_history.append(up_speed)
        self.down_history.append(down_speed)
        self.up_label.setText(f"↑  {format_speed(up_speed)}")
        self.down_label.setText(f"↓  {format_speed(down_speed)}")
        if status_text:
            self.status_label.setText(status_text)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w = self.width()
        h = self.height()

        # Background
        painter.fillRect(0, 0, w, h, QColor("#f8fafc"))

        # Find max speed for scaling
        max_val = max(max(self.up_history), max(self.down_history), 1024.0)
        n = len(self.down_history)
        step = w / max(1, n - 1)

        # Draw download curve (Blue)
        down_path = QPainterPath()
        down_path.moveTo(0, h)
        for i, val in enumerate(self.down_history):
            normalized = min(1.0, val / max_val)
            y = h - (normalized * (h - 25)) - 5
            x = i * step
            if i == 0:
                down_path.lineTo(x, y)
            else:
                down_path.lineTo(x, y)
        down_path.lineTo(w, h)
        down_path.closeSubpath()

        # Fill blue
        painter.fillPath(down_path, QBrush(QColor(14, 165, 233, 30)))
        # Stroke blue
        pen_down = QPen(QColor("#0ea5e9"), 2)
        painter.setPen(pen_down)
        for i in range(n - 1):
            y1 = h - (min(1.0, self.down_history[i] / max_val) * (h - 25)) - 5
            y2 = h - (min(1.0, self.down_history[i + 1] / max_val) * (h - 25)) - 5
            painter.drawLine(QPointF(i * step, y1), QPointF((i + 1) * step, y2))

        # Stroke upload curve (Orange)
        pen_up = QPen(QColor("#f97316"), 2)
        painter.setPen(pen_up)
        for i in range(n - 1):
            y1 = h - (min(1.0, self.up_history[i] / max_val) * (h - 25)) - 5
            y2 = h - (min(1.0, self.up_history[i + 1] / max_val) * (h - 25)) - 5
            painter.drawLine(QPointF(i * step, y1), QPointF((i + 1) * step, y2))


class NodeCard(QFrame):
    """Node card matching Clash Verge card style"""
    clicked = pyqtSignal(str)
    ping_requested = pyqtSignal(str)
    delete_requested = pyqtSignal(str)
    edit_requested = pyqtSignal(str)

    def __init__(self, node: dict, is_selected: bool = False, parent=None):
        super().__init__(parent)
        self.node_id = node.get("id", "")
        self.node_data = node
        self.is_selected = is_selected
        self.ping_ms = -2  # -2: not tested, -1: timeout, >=0: ms

        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(72)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(12)

        # Left info
        left_layout = QVBoxLayout()
        left_layout.setSpacing(4)

        self.title_label = QLabel(node.get("name", "VLESS Node"))
        self.title_label.setFont(QFont("Segoe UI", 11, QFont.Bold))
        left_layout.addWidget(self.title_label)

        # Tags / Badges
        tags_layout = QHBoxLayout()
        tags_layout.setSpacing(6)

        proto = node.get("protocol", "vless").upper()
        net = node.get("network", "tcp").upper()
        sec = node.get("security", "").upper()

        for tag in [proto, net, sec]:
            if not tag:
                continue
            lbl = QLabel(tag)
            lbl.setStyleSheet("""
                background-color: #f1f5f9;
                color: #475569;
                border-radius: 4px;
                padding: 1px 6px;
                font-size: 10px;
                font-weight: 500;
            """)
            tags_layout.addWidget(lbl)
        tags_layout.addStretch()
        left_layout.addLayout(tags_layout)

        layout.addLayout(left_layout, stretch=1)

        # Right: Ping badge and Delete button
        right_layout = QHBoxLayout()
        right_layout.setSpacing(8)

        self.ping_btn = QPushButton("测速")
        self.ping_btn.setFixedSize(68, 28)
        self.ping_btn.setCursor(Qt.PointingHandCursor)
        self.ping_btn.clicked.connect(self.on_ping_clicked)
        right_layout.addWidget(self.ping_btn)

        if self.node_id != "DIRECT":
            self.del_btn = QPushButton("✕")
            self.del_btn.setFixedSize(26, 26)
            self.del_btn.setCursor(Qt.PointingHandCursor)
            self.del_btn.setToolTip("删除此节点")
            self.del_btn.setStyleSheet("""
                QPushButton {
                    background-color: transparent;
                    color: #94a3b8;
                    border: none;
                    border-radius: 13px;
                    font-size: 13px;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background-color: #fee2e2;
                    color: #ef4444;
                }
            """)
            self.del_btn.clicked.connect(lambda: self.delete_requested.emit(self.node_id))
            right_layout.addWidget(self.del_btn)

        layout.addLayout(right_layout)

        self.update_style()

    def set_selected(self, selected: bool):
        self.is_selected = selected
        self.update_style()

    def set_ping(self, ms: int):
        self.ping_ms = ms
        if ms >= 0:
            if ms < 200:
                color = "#10b981"
            elif ms < 500:
                color = "#f59e0b"
            else:
                color = "#ef4444"
            self.ping_btn.setText(f"● {ms} ms")
            self.ping_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: transparent;
                    color: {color};
                    font-weight: bold;
                    font-size: 12px;
                    border: none;
                }}
            """)
        elif ms == -1:
            self.ping_btn.setText("● 超时")
            self.ping_btn.setStyleSheet("""
                QPushButton {
                    background-color: transparent;
                    color: #ef4444;
                    font-weight: bold;
                    font-size: 12px;
                    border: none;
                }
            """)
        elif ms == -3:
            self.ping_btn.setText("测试中..")
            self.ping_btn.setStyleSheet("""
                QPushButton {
                    background-color: transparent;
                    color: #0284c7;
                    font-size: 12px;
                    border: none;
                }
            """)
        else:
            self.ping_btn.setText("测速")
            self.ping_btn.setStyleSheet("""
                QPushButton {
                    background-color: #f1f5f9;
                    color: #64748b;
                    border-radius: 6px;
                    font-size: 11px;
                    border: 1px solid #e2e8f0;
                    padding: 2px 6px;
                }
                QPushButton:hover {
                    background-color: #e2e8f0;
                    color: #0f172a;
                }
            """)

    def update_style(self):
        if self.is_selected:
            self.setStyleSheet("""
                NodeCard {
                    background-color: #f0f9ff;
                    border: 2px solid #0284c7;
                    border-radius: 8px;
                }
            """)
            self.title_label.setStyleSheet("color: #0369a1; font-weight: bold;")
        else:
            self.setStyleSheet("""
                NodeCard {
                    background-color: #ffffff;
                    border: 1px solid #e2e8f0;
                    border-radius: 8px;
                }
                NodeCard:hover {
                    border-color: #94a3b8;
                    background-color: #f8fafc;
                }
            """)
            self.title_label.setStyleSheet("color: #1e293b; font-weight: bold;")
        self.set_ping(self.ping_ms)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.node_id)
        super().mousePressEvent(event)

    def contextMenuEvent(self, event):
        from PyQt5.QtWidgets import QMenu, QAction
        menu = QMenu(self)
        act_sel = QAction("⚡  选择此节点", self)
        act_sel.triggered.connect(lambda: self.clicked.emit(self.node_id))
        menu.addAction(act_sel)

        act_ping = QAction("🔄  测试延迟", self)
        act_ping.triggered.connect(self.on_ping_clicked)
        menu.addAction(act_ping)

        if self.node_id != "DIRECT":
            act_edit = QAction("⚙  编辑此节点", self)
            act_edit.triggered.connect(lambda: self.edit_requested.emit(self.node_id))
            menu.addAction(act_edit)

            menu.addSeparator()

            act_del = QAction("🗑️  删除此节点", self)
            act_del.triggered.connect(lambda: self.delete_requested.emit(self.node_id))
            menu.addAction(act_del)

        menu.exec_(event.globalPos())

    def on_ping_clicked(self):
        self.ping_btn.setText("测试中..")
        self.ping_requested.emit(self.node_id)
