"""Lightweight Qt plots for paired X/Y measurements."""
import math
from PySide6 import QtCore, QtGui, QtWidgets


class AxisPlot(QtWidgets.QWidget):
    def __init__(self, horizontal, vertical, signed=False):
        super().__init__()
        self.horizontal, self.vertical, self.signed = horizontal, vertical, signed
        self.points = ()
        self.setMinimumSize(165, 135)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)

    def set_points(self, points):
        self.points = tuple(points)
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QtGui.QColor("white"))
        left, top = 63, 25
        w, h = max(1, self.width()-left-18), max(1, self.height()-top-43)
        p = self.points
        xmin = p[0][0] if p else 0
        xmax = p[-1][0] if len(p) > 1 else xmin+1
        scale = max((abs(v) for _, x, y in p for v in (x, y) if math.isfinite(v)), default=1) or 1
        ymin = -scale if self.signed else 0

        def ypixel(v):
            return top+h-h*(v-ymin)/(scale-ymin)

        painter.setFont(QtGui.QFont("Segoe UI", 8))
        for i in range(5):
            x, y = round(left+i*w/4), round(top+i*h/4)
            painter.setPen(QtGui.QColor("#d7dee7"))
            painter.drawLine(x, top, x, top+h)
            painter.drawLine(left, y, left+w, y)
            painter.setPen(QtGui.QColor("#243449"))
            painter.drawText(QtCore.QRect(x-23, top+h+2, 46, 18), QtCore.Qt.AlignCenter, f"{xmin+i*(xmax-xmin)/4:.2g}")
            painter.drawText(QtCore.QRect(15, y-9, left-19, 18), QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter,
                             f"{scale-i*(scale-ymin)/4:.2g}")
        painter.drawText(QtCore.QRect(left, top+h+18, w, 21), QtCore.Qt.AlignCenter, self.horizontal)
        painter.save()
        painter.translate(12, top+h/2)
        painter.rotate(-90)
        painter.drawText(QtCore.QRectF(-h/2, -10, h, 20), QtCore.Qt.AlignCenter, self.vertical)
        painter.restore()
        for axis, color in enumerate(("#d62d39", "#1765db")):
            painter.setPen(QtGui.QPen(QtGui.QColor(color), 1.8))
            key_x = left+w-69+axis*35
            painter.drawLine(key_x, 12, key_x+12, 12)
            painter.drawText(key_x+15, 16, ("X", "Y")[axis])
            path = QtGui.QPainterPath()
            move = True
            for point in p:
                value = point[axis+1]
                if not math.isfinite(value):
                    move = True
                    continue
                x = left+w*(point[0]-xmin)/max(xmax-xmin, 1e-12)
                if move:
                    path.moveTo(x, ypixel(value))
                else:
                    path.lineTo(x, ypixel(value))
                move = False
            painter.drawPath(path)
        if not p:
            painter.setPen(QtGui.QColor("#64758a"))
            painter.drawText(QtCore.QRect(left, top, w, h), QtCore.Qt.AlignCenter, "Waiting for samples")
