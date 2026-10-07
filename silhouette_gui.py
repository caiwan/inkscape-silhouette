#!/usr/bin/env python3
# coding=utf-8
#
# Standalone Qt front end for sendto_silhouette.py.
#
# Builds the same dialog Inkscape shows for sendto_silhouette.inx (the .inx
# file is parsed at startup, so both stay in sync), lets the user open an SVG
# file and pick the layers to plot, then runs sendto_silhouette.py as a child
# process and shows its output in a console window.
#
# Licensed under CC-BY-SA-3.0 or GPL-2.0 at your choice.

import codecs
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import xml.etree.ElementTree as ElementTree
from tempfile import NamedTemporaryFile, gettempdir

from lxml import etree

try:
    from PyQt6 import QtCore, QtGui, QtWidgets
except ImportError:
    try:
        from PySide6 import QtCore, QtGui, QtWidgets
    except ImportError:
        from PyQt5 import QtCore, QtGui, QtWidgets

Qt = QtCore.Qt
Signal = getattr(QtCore, "pyqtSignal", None) or QtCore.Signal

HERE = os.path.dirname(os.path.abspath(__file__))
INX_FILE = os.path.join(HERE, "sendto_silhouette.inx")
SCRIPT_FILE = os.path.join(HERE, "sendto_silhouette.py")

INX_NS = "{http://www.inkscape.org/namespace/inkscape/extension}"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
SVG_NS = "http://www.w3.org/2000/svg"
INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"
SVG_GROUP = "{%s}g" % SVG_NS
GROUPMODE = "{%s}groupmode" % INKSCAPE_NS
LABEL = "{%s}label" % INKSCAPE_NS

# Layers sendto_silhouette.py never plots, see recursivelyTraverseSvg().
DRIVER_SKIPPED_LABELS = ("cuttingmat", "regmark", "print")
# Keep in sync with LOGFILE_DEFAULT_NAME in sendto_silhouette.py
LOGFILE_DEFAULT_NAME = "silhouette.log"

INDENT_PX = 12
LAYER_ROLE = Qt.ItemDataRole.UserRole


# --------------------------------------------------------------------------
# SVG layers
# --------------------------------------------------------------------------

def is_layer(node):
    return node.tag == SVG_GROUP and node.get(GROUPMODE) == "layer"


def is_hidden(node):
    if node.get("display") == "none":
        return True
    return re.search(r"(^|;)\s*display\s*:\s*none\s*(;|$)", node.get("style", "")) is not None


def set_hidden(node, hidden):
    if node.get("display") == "none":
        del node.attrib["display"]
    declarations = [decl for decl in node.get("style", "").split(";")
                    if decl.strip() and not re.match(r"\s*display\s*:", decl)]
    if hidden:
        declarations.append("display:none")
    if declarations:
        node.set("style", ";".join(declarations))
    elif "style" in node.attrib:
        del node.attrib["style"]


def child_layers(node):
    """Layers below `node` that have no other layer in between."""
    found = []
    for child in node:
        if not isinstance(child.tag, str):
            continue
        if is_layer(child):
            found.append(child)
        else:
            found.extend(child_layers(child))
    return found


def driver_skips(node):
    label = (node.get(LABEL) or "").lower()
    return any(word in label for word in DRIVER_SKIPPED_LABELS)


# --------------------------------------------------------------------------
# Dialog built from the .inx file
# --------------------------------------------------------------------------

class InxForm(QtWidgets.QWidget):
    """The parameter widgets described by an Inkscape .inx file."""

    def __init__(self, inx_file, parent=None):
        super().__init__(parent)
        self.getters = {}
        self.setters = {}
        root = ElementTree.parse(inx_file).getroot()
        self.title = root.findtext(INX_NS + "name", "Send to Silhouette")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        grid = QtWidgets.QGridLayout()
        layout.addLayout(grid)
        self.build(root, grid)

    def values(self):
        """All parameters in .inx order, formatted like Inkscape passes them."""
        return [(name, getter()) for name, getter in self.getters.items()]

    def set_value(self, name, value):
        if name in self.setters:
            try:
                self.setters[name](value)
            except ValueError:
                pass

    def build(self, parent, grid):
        for elem in parent:
            if elem.tag == INX_NS + "label":
                grid.addWidget(self.make_label(elem), grid.rowCount(), 0, 1, 2)
            elif elem.tag == INX_NS + "param":
                self.build_param(elem, grid)

    def make_label(self, elem):
        text = elem.text or ""
        if elem.get(XML_SPACE) == "preserve":
            text = text.strip("\n")
        else:
            text = " ".join(text.split())
        label = QtWidgets.QLabel()
        if elem.get("appearance") == "url":
            label.setText('<a href="%s">%s</a>' % (text, text))
            label.setOpenExternalLinks(True)
        else:
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setText(text)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setWordWrap(True)
        label.setContentsMargins(int(elem.get("indent", 0)) * INDENT_PX, 0, 0, 0)
        return label

    def build_param(self, elem, grid):
        name = elem.get("name")
        kind = elem.get("type")
        text = elem.get("gui-text", "")
        default = (elem.text or "").strip()
        indent = int(elem.get("indent", 0)) * INDENT_PX
        row = grid.rowCount()

        if kind == "notebook":
            tabs = QtWidgets.QTabWidget()
            pages = []
            for page in elem.findall(INX_NS + "page"):
                content = QtWidgets.QWidget()
                page_grid = QtWidgets.QGridLayout(content)
                self.build(page, page_grid)
                page_grid.setRowStretch(page_grid.rowCount(), 1)
                page_grid.setColumnStretch(1, 1)
                scroll = QtWidgets.QScrollArea()
                scroll.setWidgetResizable(True)
                scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
                scroll.setWidget(content)
                tabs.addTab(scroll, page.get("gui-text", page.get("name")))
                pages.append(page.get("name"))
            self.getters[name] = lambda: pages[tabs.currentIndex()]
            self.setters[name] = lambda v: tabs.setCurrentIndex(pages.index(v))
            grid.addWidget(tabs, row, 0, 1, 2)
            return

        if kind == "bool":
            widget = QtWidgets.QCheckBox(text)
            widget.setChecked(default == "true")
            widget.setStyleSheet("QCheckBox { margin-left: %dpx; }" % indent)
            self.getters[name] = lambda: "true" if widget.isChecked() else "false"
            self.setters[name] = lambda v: widget.setChecked(v == "true")
            grid.addWidget(widget, row, 0, 1, 2)
            return

        if kind == "float":
            widget = QtWidgets.QDoubleSpinBox()
            # Inkscape shows one decimal place unless told otherwise
            widget.setDecimals(int(elem.get("precision", 1)))
            widget.setRange(float(elem.get("min", 0)), float(elem.get("max", 10)))
            widget.setValue(float(default or 0))
            self.getters[name] = lambda: "%.*f" % (widget.decimals(), widget.value())
            self.setters[name] = lambda v: widget.setValue(float(v))
        elif kind == "int":
            widget = QtWidgets.QSpinBox()
            widget.setRange(int(elem.get("min", 0)), int(elem.get("max", 10)))
            widget.setValue(int(default or 0))
            self.getters[name] = lambda: str(widget.value())
            self.setters[name] = lambda v: widget.setValue(int(v))
        elif kind == "optiongroup":
            widget = QtWidgets.QComboBox()
            for option in elem.findall(INX_NS + "option"):
                widget.addItem((option.text or "").strip(), option.get("value"))
            widget.setSizeAdjustPolicy(
                QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            self.getters[name] = lambda: widget.currentData()
            self.setters[name] = lambda v: widget.setCurrentIndex(max(0, widget.findData(v)))
        else:  # string, path
            edit = QtWidgets.QLineEdit(default)
            self.getters[name] = edit.text
            self.setters[name] = edit.setText
            widget = edit
            if kind == "path":
                widget = QtWidgets.QWidget()
                box = QtWidgets.QHBoxLayout(widget)
                box.setContentsMargins(0, 0, 0, 0)
                button = QtWidgets.QToolButton()
                button.setText("…")
                filetypes = elem.get("filetypes", "")
                button.clicked.connect(lambda: self.browse_new_file(edit, text, filetypes))
                box.addWidget(edit)
                box.addWidget(button)

        label = QtWidgets.QLabel(text)
        label.setContentsMargins(indent, 0, 0, 0)
        label.setBuddy(widget)
        grid.addWidget(label, row, 0)
        grid.addWidget(widget, row, 1)

    def browse_new_file(self, edit, caption, filetypes):
        filters = ["*.%s" % ext for ext in filetypes.split(",") if ext]
        name_filter = "%s;;All files (*)" % " ".join(filters) if filters else "All files (*)"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, caption, edit.text(), name_filter,
            options=QtWidgets.QFileDialog.Option.DontConfirmOverwrite)
        if path:
            edit.setText(path)


# --------------------------------------------------------------------------
# Console window
# --------------------------------------------------------------------------

class Console(QtWidgets.QDialog):
    """Runs a command and shows everything it prints."""

    output = Signal(str)
    exited = Signal(int)
    running_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Silhouette console")
        self.resize(820, 420)
        self.process = None
        self.cleanup_files = []
        self.carriage_return = False
        self.log_path = None
        self.log_offset = 0
        self.log_decoder = None

        self.view = QtWidgets.QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        self.view.setFont(QtGui.QFontDatabase.systemFont(
            QtGui.QFontDatabase.SystemFont.FixedFont))

        self.stop_button = QtWidgets.QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        clear_button = QtWidgets.QPushButton("Clear")
        clear_button.clicked.connect(self.view.clear)
        close_button = QtWidgets.QPushButton("Close")
        close_button.clicked.connect(self.hide)

        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.stop_button)
        buttons.addWidget(clear_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.view)
        layout.addLayout(buttons)

        self.output.connect(self.write)
        self.exited.connect(self.on_exited)
        # Only used where the child has no terminal to write its log to.
        self.log_timer = QtCore.QTimer(self)
        self.log_timer.setInterval(200)
        self.log_timer.timeout.connect(self.read_log)

    def is_running(self):
        return self.process is not None

    def run(self, command, log_path, cleanup_files=()):
        self.cleanup_files = list(cleanup_files)
        self.write("$ %s\n" % shlex.join(command))
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        try:
            if os.name == "posix":
                # sendto_silhouette.py writes its log and progress to /dev/tty.
                # Give it a pseudo terminal of its own so that we receive that
                # too, together with stdout and stderr.
                import pty
                master, slave = pty.openpty()
                try:
                    self.process = subprocess.Popen(
                        command, stdin=slave, stdout=slave, stderr=slave, env=env,
                        close_fds=True, preexec_fn=self.make_controlling_tty)
                finally:
                    os.close(slave)
                stream = master
            else:
                # No /dev/tty here: the log only ends up in the log file.
                self.process = subprocess.Popen(
                    command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, env=env)
                stream = self.process.stdout.fileno()
                self.log_path = log_path
                self.log_offset = 0
                self.log_decoder = codecs.getincrementaldecoder("utf-8")("replace")
                self.log_timer.start()
        except OSError as error:
            self.write("Could not start: %s\n" % error)
            self.remove_cleanup_files()
            return
        self.stop_button.setEnabled(True)
        self.running_changed.emit(True)
        threading.Thread(target=self.pump, args=(self.process, stream), daemon=True).start()

    @staticmethod
    def make_controlling_tty():
        import fcntl
        import termios
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    def pump(self, process, stream):
        """Reader thread: forward child output to the GUI thread."""
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        while True:
            try:
                data = os.read(stream, 4096)
            except OSError:  # EIO: the child closed its side of the pty
                data = b""
            if not data:
                break
            self.output.emit(decoder.decode(data))
        if os.name == "posix":
            os.close(stream)
        self.exited.emit(process.wait())

    def read_log(self):
        try:
            with open(self.log_path, "rb") as log:
                log.seek(self.log_offset)
                data = log.read()
        except OSError:
            return
        self.log_offset += len(data)
        if data:
            self.write(self.log_decoder.decode(data))

    def write(self, text):
        cursor = self.view.textCursor()
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
        for part in re.split(r"(\r\n|\n|\r)", text):
            if part == "\r":
                self.carriage_return = True
            elif part in ("\n", "\r\n"):
                self.carriage_return = False
                cursor.insertText("\n")
            elif part:
                if self.carriage_return:
                    # overwrite the current line, like a terminal does
                    cursor.movePosition(QtGui.QTextCursor.MoveOperation.StartOfBlock,
                                        QtGui.QTextCursor.MoveMode.KeepAnchor)
                    cursor.removeSelectedText()
                    self.carriage_return = False
                cursor.insertText(part)
        self.view.setTextCursor(cursor)
        self.view.ensureCursorVisible()

    def on_exited(self, returncode):
        if self.log_timer.isActive():
            self.log_timer.stop()
            self.read_log()
        self.process = None
        self.carriage_return = False
        self.write("\n[exit code %d]\n\n" % returncode)
        self.remove_cleanup_files()
        self.stop_button.setEnabled(False)
        self.running_changed.emit(False)

    def remove_cleanup_files(self):
        for path in self.cleanup_files:
            try:
                os.remove(path)
            except OSError:
                pass
        self.cleanup_files = []

    def stop(self):
        if self.process is None:
            return
        try:
            if os.name == "posix":
                # the child leads its own session, see make_controlling_tty()
                os.killpg(self.process.pid, signal.SIGTERM)
            else:
                self.process.terminate()
        except OSError:
            pass


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------

class MainWindow(QtWidgets.QWidget):

    def __init__(self):
        super().__init__()
        self.svg_tree = None
        self.settings = QtCore.QSettings("inkscape-silhouette", "silhouette_gui")
        self.form = InxForm(INX_FILE)
        self.console = Console(self)
        self.console.setWindowFlag(Qt.WindowType.Window)
        self.setWindowTitle(self.form.title)

        self.file_edit = QtWidgets.QLineEdit()
        self.file_edit.setPlaceholderText("SVG file")
        self.file_edit.editingFinished.connect(self.load_svg)
        open_button = QtWidgets.QPushButton("Open…")
        open_button.clicked.connect(self.browse_svg)
        reload_button = QtWidgets.QPushButton("Reload")
        reload_button.clicked.connect(lambda: self.load_svg(force=True))
        file_row = QtWidgets.QHBoxLayout()
        file_row.addWidget(self.file_edit, 1)
        file_row.addWidget(open_button)
        file_row.addWidget(reload_button)

        self.layers = QtWidgets.QTreeWidget()
        self.layers.setHeaderLabel("Layers to plot")
        self.layer_hint = QtWidgets.QLabel()
        self.layer_hint.setWordWrap(True)
        layer_panel = QtWidgets.QWidget()
        layer_layout = QtWidgets.QVBoxLayout(layer_panel)
        layer_layout.setContentsMargins(0, 0, 0, 0)
        layer_layout.addWidget(self.layers, 1)
        layer_layout.addWidget(self.layer_hint)
        self.layers.itemChanged.connect(lambda *_: self.layers_changed())

        splitter = QtWidgets.QSplitter()
        splitter.addWidget(layer_panel)
        splitter.addWidget(self.form)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 640])

        console_button = QtWidgets.QPushButton("Console")
        console_button.clicked.connect(self.show_console)
        close_button = QtWidgets.QPushButton("Close")
        close_button.clicked.connect(self.close)
        self.apply_button = QtWidgets.QPushButton("Apply")
        self.apply_button.setDefault(True)
        self.apply_button.clicked.connect(self.apply)
        self.console.running_changed.connect(
            lambda running: self.apply_button.setEnabled(not running))
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(console_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        buttons.addWidget(self.apply_button)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(file_row)
        layout.addWidget(splitter, 1)
        layout.addLayout(buttons)
        self.resize(900, 720)

        self.loaded_path = None
        self.loaded_mtime = None
        self.restore_settings()
        self.layers_changed()

    # ---- settings --------------------------------------------------------

    def restore_settings(self):
        for name, _ in self.form.values():
            value = self.settings.value("params/" + name)
            if value is not None:
                self.form.set_value(name, str(value))
        last_file = self.settings.value("file")
        if last_file and os.path.isfile(str(last_file)):
            self.open_svg(str(last_file))

    def save_settings(self):
        for name, value in self.form.values():
            self.settings.setValue("params/" + name, value)
        self.settings.setValue("file", self.file_edit.text())

    def closeEvent(self, event):
        if self.console.is_running():
            answer = QtWidgets.QMessageBox.question(
                self, self.windowTitle(),
                "The cutter job is still running. Stop it and quit?")
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.console.stop()
        self.save_settings()
        event.accept()

    # ---- SVG file and layers ---------------------------------------------

    def browse_svg(self):
        start = os.path.dirname(self.file_edit.text())
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open SVG file", start, "SVG files (*.svg);;All files (*)")
        if path:
            self.open_svg(path)

    def open_svg(self, path):
        self.file_edit.setText(path)
        self.load_svg()

    def load_svg(self, force=False):
        path = self.file_edit.text().strip()
        if path == self.loaded_path and not force:
            return
        self.loaded_path = path
        self.loaded_mtime = None
        self.svg_tree = None
        self.layers.clear()
        if path:
            try:
                self.loaded_mtime = os.path.getmtime(path)
                self.svg_tree = etree.parse(path)
            except (OSError, etree.XMLSyntaxError) as error:
                QtWidgets.QMessageBox.warning(
                    self, self.windowTitle(), "Cannot read %s:\n%s" % (path, error))
        if self.svg_tree is not None:
            self.layers.blockSignals(True)
            self.add_layers(self.layers.invisibleRootItem(), self.svg_tree.getroot())
            self.layers.blockSignals(False)
            self.layers.expandAll()
        self.layers_changed()

    def reload_if_changed(self):
        """Pick up edits saved by another program, keeping the layer choice."""
        try:
            if os.path.getmtime(self.loaded_path) == self.loaded_mtime:
                return
        except OSError:
            pass
        path_of = self.svg_tree.getpath
        states = {path_of(item.data(0, LAYER_ROLE)): item.checkState(0)
                  for item in self.layer_items()}
        self.load_svg(force=True)
        if self.svg_tree is None:
            return
        path_of = self.svg_tree.getpath
        for item in self.layer_items():
            state = states.get(path_of(item.data(0, LAYER_ROLE)))
            if state is not None:
                item.setCheckState(0, state)

    def add_layers(self, parent_item, node):
        # topmost layer first, like the Inkscape layers dialog
        for layer in reversed(child_layers(node)):
            skipped = driver_skips(layer)
            name = layer.get(LABEL) or layer.get("id") or "(unnamed layer)"
            if skipped:
                name += "  [never plotted]"
            item = QtWidgets.QTreeWidgetItem(parent_item, [name])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            if skipped:
                item.setToolTip(0, "sendto_silhouette.py skips cutting mat, "
                                   "registration mark and print layers.")
            item.setData(0, LAYER_ROLE, layer)
            visible = not skipped and not is_hidden(layer)
            item.setCheckState(
                0, Qt.CheckState.Checked if visible else Qt.CheckState.Unchecked)
            if not skipped:
                self.add_layers(item, layer)

    def layer_items(self, item=None):
        item = item or self.layers.invisibleRootItem()
        found = []
        for child in (item.child(i) for i in range(item.childCount())):
            found.append(child)
            found.extend(self.layer_items(child))
        return found

    def changed_layers(self):
        """(layer, plot it?) for every layer whose checkbox disagrees with the file."""
        changed = []
        for item in self.layer_items():
            layer = item.data(0, LAYER_ROLE)
            checked = item.checkState(0) == Qt.CheckState.Checked
            if not driver_skips(layer) and checked == is_hidden(layer):
                changed.append((layer, checked))
        return changed

    def layers_changed(self):
        # Like layer visibility in Inkscape: an unchecked layer takes its
        # sublayers with it.
        self.layers.blockSignals(True)
        self.enable_layers(self.layers.invisibleRootItem(), True)
        self.layers.blockSignals(False)
        if self.svg_tree is None:
            hint = "No SVG file loaded."
        elif self.layers.topLevelItemCount() == 0:
            hint = "No layers: the whole document is plotted."
        else:
            hint = "Checked layers are plotted. Hidden layers start unchecked."
        self.layer_hint.setText(hint)

    def enable_layers(self, item, enabled):
        for child in (item.child(i) for i in range(item.childCount())):
            skipped = driver_skips(child.data(0, LAYER_ROLE))
            child.setDisabled(skipped or not enabled)
            self.enable_layers(
                child, enabled and child.checkState(0) == Qt.CheckState.Checked)

    def prepare_input(self):
        """Return (svg file, temporary files) for the current layer choice."""
        changed = self.changed_layers()
        if not changed:
            return self.loaded_path, []
        # The driver plots whatever is visible, so hand it a copy of the
        # document with layer visibility set as chosen here.
        for layer, visible in changed:
            set_hidden(layer, not visible)
        try:
            with NamedTemporaryFile(suffix=".svg", prefix="silhouette-gui-",
                                    delete=False) as tmp:
                self.svg_tree.write(tmp, xml_declaration=True, encoding="UTF-8")
        finally:
            for layer, visible in changed:
                set_hidden(layer, visible)
        return tmp.name, [tmp.name]

    # ---- running ---------------------------------------------------------

    def show_console(self):
        self.console.show()
        self.console.raise_()

    def apply(self):
        if self.console.is_running():
            return
        self.load_svg()
        if self.svg_tree is not None:
            self.reload_if_changed()
        if self.svg_tree is None:
            QtWidgets.QMessageBox.information(
                self, self.windowTitle(), "Open an SVG file first.")
            return
        svg_file, temporary = self.prepare_input()
        self.save_settings()

        values = self.form.values()
        command = [sys.executable, "-u", SCRIPT_FILE]
        command += ["--%s=%s" % (name, value) for name, value in values]
        command.append(svg_file)
        log_path = dict(values).get("logfile") or os.path.join(gettempdir(), LOGFILE_DEFAULT_NAME)

        self.show_console()
        self.console.run(command, log_path, temporary)


def main():
    app = QtWidgets.QApplication(sys.argv)
    window = MainWindow()
    arguments = app.arguments()[1:]
    if arguments:
        window.open_svg(os.path.abspath(arguments[0]))
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
