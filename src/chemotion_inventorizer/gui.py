"""Native desktop inventory window and UI behavior."""
import json
import os
import re
import subprocess
import tempfile
import threading
import webbrowser
import base64
from collections import deque
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk
from . import APP_NAME, client
from .settings import (
    clear_credentials,
    column_order,
    print_preset,
    save_credentials,
    save_profile_settings,
    saved_credentials,
    saved_profile_settings,
    visible_columns,
)
from .inventory import (
    INVENTORY_HEADINGS,
    INVENTORY_KEYS,
    PRINT_CHECKLIST_HEADINGS,
    PRINT_CHECKLIST_KEYS,
    apply_molecule_name_update,
    collection_picker_label,
    displayed_row_value,
    format_amount_with_unit,
    is_zero_amount,
    last_modified_sort_value,
    location_choices,
    natural_sort_value,
    parse_amount,
    refresh_inventory_row,
    rows_for_location_filter,
    shown,
    structure_image_request_path,
    synchronized_collection_source,
)
from .client import (
    ChemotionClient,
    DEFAULT_CHEMOTION_URL,
    InventoryLoadCancelled,
    normalized_chemotion_url,
)
from .printing import (
    write_inventory_pdf,
)


def multi_selection_modifier(windowing_system):
    """Return the platform's standard modifier for extending a selection."""
    return "Command" if windowing_system == "aqua" else "Control"


def minimum_window_size(screen_width, screen_height):
    """Leave room for window decorations and the taskbar on small displays."""
    return min(1050, max(320, screen_width - 80)), min(620, max(240, screen_height - 160))


def prevent_combobox_scroll_selection(_event=None):
    """Keep the focused combobox value unchanged while the user scrolls."""
    return "break"


def disable_combobox_scroll_selection(combobox):
    """Prevent wheel events from selecting a different combobox item."""
    for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        combobox.bind(sequence, prevent_combobox_scroll_selection)


def inverted_structure_png(image_data, width, height):
    """Invert thumbnail RGB channels while preserving transparency."""
    import resvg_py

    encoded = base64.b64encode(image_data).decode("ascii")
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">'
        '<defs><filter id="invert" color-interpolation-filters="sRGB">'
        '<feComponentTransfer>'
        '<feFuncR type="linear" slope="-1" intercept="1"/>'
        '<feFuncG type="linear" slope="-1" intercept="1"/>'
        '<feFuncB type="linear" slope="-1" intercept="1"/>'
        '</feComponentTransfer></filter></defs>'
        f'<image width="{width}" height="{height}" filter="url(#invert)" '
        f'href="data:image/png;base64,{encoded}"/></svg>'
    )
    return resvg_py.svg_to_bytes(svg_string=svg, width=width, height=height)


def system_dark_mode():
    try: result = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True, timeout=1)
    except (OSError, subprocess.TimeoutExpired): return False
    return result.returncode == 0 and result.stdout.strip().casefold() == "dark"


def system_accent_color():
    """Return the current macOS accent colour, falling back to system blue."""
    colors = {-1: "#8e8e93", 0: "#ff3b30", 1: "#ff9500", 2: "#ffcc00",
              3: "#34c759", 4: "#007aff", 5: "#af52de", 6: "#ff2d55"}
    try:
        result = subprocess.run(["defaults", "read", "-g", "AppleAccentColor"], capture_output=True, text=True, timeout=1)
        return colors.get(int(result.stdout.strip()), colors[4])
    except (OSError, ValueError, subprocess.TimeoutExpired): return colors[4]


def contrasting_text_color(color):
    """Choose readable text for a solid RGB selection colour."""
    red, green, blue = (int(color[index:index + 2], 16) for index in (1, 3, 5))
    luminance = (0.299 * red) + (0.587 * green) + (0.114 * blue)
    return "#000000" if luminance > 160 else "#ffffff"


def status_text_color(dark):
    """Keep status messages legible against the current window appearance."""
    return "#f2f2f7" if dark else "#000000"


class StatusLabel(tk.Label):
    """Keep the progress status message readable in either appearance."""
    def configure(self, cnf=None, **kwargs):
        window = self.winfo_toplevel()
        kwargs["fg"] = status_text_color(getattr(window, "dark_mode", False))
        return super().configure(cnf, **kwargs)


def column_drop_marker_x(tree, event):
    """Measure the heading's left edge even when the first row is offscreen."""
    column = tree.identify_column(event.x)
    if not column or column == "#0":
        return None
    for y in range(event.y, tree.winfo_height()):
        row = tree.identify_row(y)
        if row:
            bounds = tree.bbox(row, column)
            if bounds:
                return bounds[0]
            break
    # Empty tables still have laid-out column widths and a scroll position.
    displayed = tree.cget("displaycolumns")
    if isinstance(displayed, str):
        displayed = tree.tk.splitlist(displayed)
    if tuple(displayed) == ("#all",):
        displayed = tree.cget("columns")
        if isinstance(displayed, str):
            displayed = tree.tk.splitlist(displayed)
    widths = [tree.column(f"#{index + 1}", "width") for index in range(len(displayed))]
    inset = next((x for x in range(tree.winfo_width())
                  if tree.identify_region(x, event.y) in {"heading", "separator"}), 0)
    tree_column_width = tree.column("#0", "width") if "tree" in tree.cget("show") else 0
    return inset + tree_column_width + sum(widths[:int(column[1:]) - 1]) - round(tree.xview()[0] * sum(widths))


class ColumnDragFeedback:
    """Show the prospective column drop point."""
    def __init__(self, tree, color):
        self.tree = tree
        self.color = color
        self.marker = tk.Frame(tree.master, bg=color, width=3)

    def show(self, event):
        """Place a visible insertion line."""
        marker_x = column_drop_marker_x(self.tree, event)
        if marker_x is None or not 0 <= marker_x < self.tree.winfo_width():
            self.marker.place_forget()
            return
        self.marker.place(x=self.tree.winfo_x() + marker_x - 1,
                          y=self.tree.winfo_y(), height=26)
        self.marker.lift()

    def hide(self):
        self.marker.place_forget()


class Inventorizer(tk.Tk):
    headings = INVENTORY_HEADINGS
    keys = INVENTORY_KEYS
    column_menu_order = ("structure", "inventory_sample", "label", "location", "decoupled", "molecule", "density", "sample", "vendor", "amount", "last_modified", "created")
    default_column_order = ("structure", "inventory_sample", "label", "location", "decoupled", "molecule", "density", "sample", "vendor", "amount", "last_modified", "created")
    default_visible_columns = tuple(key for key in default_column_order if key not in {"decoupled", "created"})
    column_widths = {"label":120,"structure":85,"molecule":250,"density":100,"sample":200,"vendor":135,"amount":100,"location":260,"last_modified":145,"created":145,"decoupled":95,"inventory_sample":85}
    column_min_widths = {"label":85,"structure":70,"molecule":120,"density":70,"sample":110,"vendor":90,"amount":70,"location":120,"last_modified":105,"created":105,"decoupled":70,"inventory_sample":70}
    palettes = {False: {"base":"#f4f7f6","panel":"#ffffff","text":"#17211f","muted":"#52616c","accent":"#08756a","error":"#b42318"}, True: {"base":"#1c1c1e","panel":"#2c2c2e","text":"#f2f2f7","muted":"#aeaeb2","accent":"#41c7b3","error":"#ff6961"}}
    def __init__(self):
        username, password = saved_credentials(); client.CHEMOTION_USERNAME, client.CHEMOTION_PASSWORD = username, password
        super().__init__(); self.title(APP_NAME); self.minsize(1050, 620); self.rows = []; self.selected_row_ids = set(); self.location_filter_retained_row_ids = set(); self.sort_key = "label"; self.sort_reverse = False; self.location_filter = tk.StringVar(value="All locations"); self.collection_value = tk.StringVar(value="Choose collection…"); self.collection_choices = {}; self.logged_in = False; self.profile_settings = saved_profile_settings(); saved_server = self.profile_settings.get("chemotion_server", DEFAULT_CHEMOTION_URL); self.server_value = tk.StringVar(value=saved_server if isinstance(saved_server, str) else DEFAULT_CHEMOTION_URL); self.username_value = tk.StringVar(value=username); self.password_value = tk.StringVar(value=password); self.remember_credentials = tk.BooleanVar(value=True); self.amount_editor = None; self.editing_cell = None; self.edit_after_save = None; self.editing_rows = []; self.inventory_cancel_event = None; self.inventory_loading = False
        try: client.CHEMOTION_URL = normalized_chemotion_url(self.server_value.get())
        except ValueError: self.server_value.set(DEFAULT_CHEMOTION_URL); client.CHEMOTION_URL = DEFAULT_CHEMOTION_URL
        self.column_order = column_order(self.profile_settings.get("column_order"), self.default_column_order)
        selected_columns = visible_columns(self.profile_settings.get("visible_columns", self.default_visible_columns), self.column_order)
        self.column_visibility = {key: tk.BooleanVar(value=key in selected_columns) for key in self.keys}
        self.server_value.trace_add("write", self.connection_details_changed); self.username_value.trace_add("write", self.connection_details_changed); self.password_value.trace_add("write", self.connection_details_changed)
        icon_path = os.environ.get("CHEMOTION_INVENTORIZER_ICON")
        if icon_path and Path(icon_path).is_file():
            self.app_icon = tk.PhotoImage(file=icon_path)
            self.iconphoto(True, self.app_icon)
        self.dark_mode = system_dark_mode(); self.style = ttk.Style(self); self.colors = self.appearance_colors(self.dark_mode)
        self.sample_list_font = tkfont.nametofont("TkDefaultFont").copy()
        saved_font_size = self.profile_settings.get("table_font_size", 18)
        if type(saved_font_size) is not int or not 8 <= saved_font_size <= 48:
            saved_font_size = 18
        self.sample_list_font.configure(size=saved_font_size)
        self.font_size_value = tk.StringVar(value=str(saved_font_size))
        self.structure_size = self.profile_settings.get("structure_size", 50)
        if type(self.structure_size) is not int or not 20 <= self.structure_size <= 200:
            self.structure_size = 50
        self.structure_size_value = tk.StringVar(value=str(self.structure_size))
        self.structure_svg_data = {}
        self.column_header_font = tkfont.nametofont("TkDefaultFont").copy()
        self.column_header_font.configure(size=12)
        self.minsize(*minimum_window_size(self.winfo_screenwidth(), self.winfo_screenheight()))
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.top = tk.Frame(self, padx=24, pady=18); self.top.grid(row=0, column=0, sticky="ew")
        self.controls = tk.Frame(self.top); self.controls.pack(anchor="w", pady=(8, 0))
        self.login_controls = tk.Frame(self.controls); self.login_controls.pack(anchor="w")
        self.location_controls = tk.Frame(self.controls); self.location_controls.pack(anchor="w", pady=(6, 0))

        ttk.Label(self.login_controls, text="Server:").pack(side="left", padx=(4,0)); self.server_entry = ttk.Entry(self.login_controls, textvariable=self.server_value, width=28); self.server_entry.pack(side="left", padx=4); ttk.Label(self.login_controls, text="Username:").pack(side="left", padx=(4,0)); self.username_entry = ttk.Entry(self.login_controls, textvariable=self.username_value, width=9); self.username_entry.pack(side="left", padx=4); ttk.Label(self.login_controls, text="Password:").pack(side="left"); self.password_entry = ttk.Entry(self.login_controls, textvariable=self.password_value, width=21, show="•"); self.password_entry.pack(side="left", padx=4); self.remember_checkbutton = ttk.Checkbutton(self.login_controls, text="Remember", variable=self.remember_credentials, command=self.remember_credentials_changed); self.remember_checkbutton.pack(side="left", padx=(0, 4)); self.login_button = ttk.Button(self.login_controls, text="Sign in", command=self.login); self.login_button.pack(side="left", padx=(0, 12)); self.server_entry.bind("<Return>", self.login_from_return); self.username_entry.bind("<Return>", self.login_from_return); self.password_entry.bind("<Return>", self.login_from_return); ttk.Label(self.login_controls, text="Collection:").pack(side="left", padx=(4,0)); self.collection_menu = ttk.Combobox(self.login_controls, textvariable=self.collection_value, values=(), state="disabled", width=20); self.collection_menu.pack(side="left", padx=4); self.collection_menu.bind("<<ComboboxSelected>>", self.load_selected_collection); disable_combobox_scroll_selection(self.collection_menu); self.cancel_load_button = ttk.Button(self.login_controls, text="Cancel", command=self.cancel_inventory_load, state="disabled"); self.cancel_load_button.pack(side="left", padx=(0, 4)); self.refresh_button = ttk.Button(self.login_controls, text="Refresh", command=self.refresh); self.refresh_button.pack(side="left", padx=4)

        self.columns_button = ttk.Menubutton(self.location_controls, text="Columns")
        self.columns_menu = tk.Menu(self.columns_button, tearoff=False)
        self.columns_button.configure(menu=self.columns_menu); self.columns_button.pack(side="left", padx=4)
        ttk.Label(self.location_controls, text="Structure size:").pack(side="left", padx=(12, 0))
        self.structure_size_spinbox = ttk.Spinbox(self.location_controls, from_=20, to=200,
                                                 textvariable=self.structure_size_value, width=3)
        self.structure_size_spinbox.pack(side="left", padx=4)
        self.structure_size_spinbox.bind("<Return>", self.finish_structure_size_edit)
        self.structure_size_spinbox.bind("<FocusOut>", self.finish_structure_size_edit)
        ttk.Label(self.location_controls, text="Font size:").pack(side="left", padx=(12, 0))
        self.font_size_spinbox = ttk.Spinbox(self.location_controls, from_=8, to=48,
                                             textvariable=self.font_size_value, width=3)
        self.font_size_spinbox.pack(side="left", padx=4)
        self.font_size_spinbox.bind("<Return>", self.finish_font_size_edit)
        self.font_size_spinbox.bind("<FocusOut>", self.finish_font_size_edit)
        column_labels = dict(zip(self.keys, self.headings))
        selector_keys = self.column_menu_order
        for key in selector_keys: self.columns_menu.add_checkbutton(label=column_labels[key], variable=self.column_visibility[key], command=lambda key=key: self.toggle_column(key))
        ttk.Label(self.location_controls, text="Location:").pack(side="left", padx=(12,0)); self.location_filter_menu = ttk.Combobox(self.location_controls, textvariable=self.location_filter, values=("All locations",), state="readonly", width=18); self.location_filter_menu.pack(side="left", padx=4); self.location_filter_menu.bind("<<ComboboxSelected>>", self.filter_by_location); disable_combobox_scroll_selection(self.location_filter_menu)
        self.print_button = ttk.Button(self.location_controls, text="Print…", command=self.printable, state="disabled"); self.print_button.pack(side="left", padx=4)
        self.export_button = ttk.Button(self.location_controls, text="Export collection…", command=self.export_collection, state="disabled")
        self.export_button.pack(side="left", padx=4)
        self.export_status = ttk.Label(self.location_controls, text="")
        self.export_status.pack(side="left", padx=4)
        self.queued_exports = 0
        self.structure_image_variants = {}
        self.structure_images = {}; self.structure_image_pending = set(); self.structure_image_queue = deque(); self.structure_image_queue_lock = threading.Lock(); self.structure_image_worker_active = False; self.structure_image_waiters = {}; self.structure_rows_pending = set()
        frame = ttk.Frame(self, padding=(12, 12, 12, 0), style="Inventory.TFrame"); frame.grid(row=1, column=0, sticky="nsew", padx=24); self.tree = ttk.Treeview(frame, columns=self.keys, displaycolumns=self.displayed_data_column_keys(), show="tree headings" if self.column_visibility["structure"].get() else "headings", style="Inventory.Treeview", selectmode="extended")
        self.tree.bind("<<TreeviewSelect>>", self.remember_selection)
        self.tree.bind("<Command-a>", self.select_all_visible)
        self.tree.bind("<Delete>", self.delete_selected_row)
        self.tree.bind(f"<{multi_selection_modifier(self.tk.call('tk', 'windowingsystem'))}-Button-1>", self.extend_edit_selection)
        self.tree.bind("<Shift-Button-1>", self.extend_edit_selection)
        self.tree.bind("<ButtonPress-1>", self.begin_column_drag)
        self.tree.bind("<B1-Motion>", self.move_column_drag)
        self.tree.bind("<ButtonRelease-1>", self.finish_column_drag)
        self.tree.bind("<Button-1>", self.edit_amount, add="+")
        self.tree.bind("<Double-1>", self.open_sample)
        self.tree.bind("<Double-1>", self.auto_size_column, add="+")
        # Header clicks are handled by finish_column_drag so the same gesture
        # can distinguish sorting from column reordering. Registering a
        # Treeview heading command as well would toggle the sort direction a
        # second time on a normal click.
        for heading, key in zip(self.headings, self.keys): self.tree.heading(key, text=heading)
        self.tree.heading("#0", text="Structure" if self.column_visibility["structure"].get() else "")
        self.tree.column("#0", width=round(self.structure_size * 1.5) + 10 if self.column_visibility["structure"].get() else 0,
                         minwidth=round(self.structure_size * 1.5) + 10 if self.column_visibility["structure"].get() else 0,
                         stretch=False, anchor="center")
        self.style.configure("Inventory.Treeview", font=self.sample_list_font, rowheight=self.table_row_height(), indent=0, imageanchor="center")
        self.font_size_value.trace_add("write", self.change_font_size)
        self.structure_size_value.trace_add("write", self.change_structure_size)
        # The native tree layout reserves space for an expander and left-aligns
        # item images, independently of the column's text anchor. Tk 9 also
        # needs imageanchor above because it overrides the layout's sticky.
        self.style.layout("Inventory.Treeview.Item", [
            ("Treeitem.padding", {"sticky": "nswe", "children": [
                ("Treeitem.image", {"sticky": ""}),
            ]}),
        ])
        self.update_sort_headings()
        for key, width in self.column_widths.items(): self.tree.column(key, width=width, minwidth=self.column_min_widths[key], stretch=False, anchor="center" if key in {"decoupled", "inventory_sample"} else "w")
        self.restore_column_widths()
        vertical = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview); horizontal = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview); self.vertical_scrollbar = vertical; self.horizontal_scrollbar = horizontal; self.tree.configure(yscrollcommand=self.tree_yview_changed, xscrollcommand=self.tree_xview_changed)
        self.tree.grid(row=0,column=0,sticky="nsew"); vertical.grid(row=0,column=1,sticky="ns"); horizontal.grid(row=1,column=0,sticky="ew"); frame.columnconfigure(0,weight=1); frame.rowconfigure(0,weight=1)
        self.progress_area = tk.Frame(self, padx=24); self.progress_area.grid(row=2, column=0, sticky="ew", pady=(0, 18))
        self.status_area = tk.Frame(self.progress_area); self.status_area.pack(side="left", padx=(0, 12))
        self.status = StatusLabel(self.status_area, text="0 samples"); self.status.pack(anchor="w")
        self.progress = ttk.Progressbar(self.progress_area, mode="determinate", maximum=100, style="Inventory.Horizontal.TProgressbar")
        self.progress.pack(side="left", fill="x", expand=True)
        self.cancel_load_button.destroy()
        self.cancel_load_button = ttk.Button(self.progress_area, text="Stop", command=self.cancel_inventory_load, state="disabled")
        self.cancel_load_button.pack(side="right", padx=(8, 0))
        self.bind("<Escape>", lambda _event: self.cancel_load_button.invoke(), add="+")
        self.apply_appearance(True); self.watch_system_appearance()
        self.after_idle(lambda: self.state("zoomed"))
        if username and password and client.CHEMOTION_URL != DEFAULT_CHEMOTION_URL:
            # Defer until the window has been built so the app can paint before
            # the network request begins.
            self.status.configure(text="Signing in to Chemotion…", fg=self.colors["accent"])
            self.after_idle(self.login)
        else:
            self.status.configure(text="Enter your Chemotion credentials and select Sign in.", fg=self.colors["muted"])
    def apply_appearance(self, force=False):
        dark = system_dark_mode()
        accent = system_accent_color()
        if not force and dark == self.dark_mode and accent == self.colors.get("accent"): return
        self.dark_mode = dark; self.colors = self.appearance_colors(dark); self.colors["accent"] = accent; c = self.colors; self.configure(bg=c["base"])
        for item in (self.top, self.status_area, self.controls, self.login_controls, self.location_controls, self.progress_area): item.configure(bg=c["base"])
        self.status.configure(bg=c["base"])
        self.style.configure("Inventory.Horizontal.TProgressbar", background=accent, troughcolor=c["panel"])
        row_height = self.table_row_height()
        self.style.configure("Inventory.TFrame",background=c["base"]); self.style.configure("Inventory.Treeview",background=c["panel"],fieldbackground=c["panel"],foreground=c["text"],rowheight=row_height); self.style.map("Inventory.Treeview",background=[("selected",accent)],foreground=[("selected",contrasting_text_color(accent))]); self.style.configure("Inventory.Treeview.Heading",background=c["base"],foreground=c["text"],font=self.column_header_font)
        self.tree.tag_configure("alternate", background="#38383a" if dark else "#eeeeee")
        self.refresh_structure_appearance()
    def refresh_structure_appearance(self):
        for source, variants in self.structure_image_variants.items():
            self.structure_images[source] = variants[bool(self.dark_mode)]
        for row in self.rows:
            image = self.structure_images.get(row.get("structure"))
            if image is not None and self.tree.exists(str(row["id"])):
                self.tree.item(str(row["id"]), image=image)
    def appearance_colors(self, dark):
        """Use the native label surface for the surrounding window as well."""
        colors = self.palettes[dark].copy()
        colors["base"] = self.style.lookup("TLabel", "background") or colors["base"]
        return colors
    def watch_system_appearance(self): self.apply_appearance(); self.after(1500,self.watch_system_appearance)
    def visible_column_keys(self):
        return tuple(key for key in self.column_order if self.column_visibility[key].get())
    def displayed_data_column_keys(self):
        return tuple(key for key in self.visible_column_keys() if key != "structure")
    def configure_visible_columns(self):
        structure_visible = self.column_visibility["structure"].get()
        self.tree.configure(displaycolumns=self.displayed_data_column_keys(),
                            show="tree headings" if structure_visible else "headings")
        self.tree.heading("#0", text="Structure" if structure_visible else "")
        self.tree.column("#0", width=round(self.structure_size * 1.5) + 10 if structure_visible else 0,
                         minwidth=round(self.structure_size * 1.5) + 10 if structure_visible else 0,
                         stretch=False, anchor="center")
        self.style.configure("Inventory.Treeview", rowheight=self.table_row_height())
        self.restore_column_widths()
    def restore_column_widths(self):
        widths = self.profile_settings.get("column_widths", {})
        if not isinstance(widths, dict):
            return
        for key, width in widths.items():
            if key not in self.column_min_widths or type(width) is not int or width <= 0:
                continue
            if key == "structure" and not self.column_visibility[key].get():
                continue
            column = "#0" if key == "structure" else key
            minimum = self.tree.column(column, "minwidth")
            self.tree.column(column, width=max(minimum, width))
    def remember_column_widths(self):
        widths = self.profile_settings.get("column_widths", {})
        widths = dict(widths) if isinstance(widths, dict) else {}
        for key in self.visible_column_keys():
            widths[key] = self.tree.column("#0" if key == "structure" else key, "width")
        self.profile_settings["column_widths"] = widths
        save_profile_settings(self.profile_settings)
    def table_row_height(self):
        return max(self.structure_size + 6 if self.column_visibility["structure"].get() else 34,
                   self.sample_list_font.metrics("linespace") + 10)
    def change_structure_size(self, *_args):
        try:
            size = int(self.structure_size_value.get())
        except ValueError:
            return
        if not 20 <= size <= 200 or size == self.structure_size:
            return
        self.structure_size = size
        if self.column_visibility["structure"].get():
            self.tree.column("#0", width=round(size * 1.5) + 10,
                             minwidth=round(size * 1.5) + 10)
        self.style.configure("Inventory.Treeview", rowheight=self.table_row_height())
        for source, svg_data in list(self.structure_svg_data.items()):
            self._structure_image_loaded(source, svg_data)
        self.profile_settings["structure_size"] = size
        save_profile_settings(self.profile_settings)
    def finish_structure_size_edit(self, _event=None):
        self.structure_size_value.set(str(self.structure_size))
    def change_font_size(self, *_args):
        try:
            size = int(self.font_size_value.get())
        except ValueError:
            return
        if not 8 <= size <= 48:
            return
        self.sample_list_font.configure(size=size)
        self.style.configure("Inventory.Treeview", rowheight=self.table_row_height())
        self.profile_settings["table_font_size"] = size
        save_profile_settings(self.profile_settings)
    def finish_font_size_edit(self, _event=None):
        self.font_size_value.set(str(self.sample_list_font.cget("size")))
    def toggle_column(self, key):
        if not self.visible_column_keys():
            self.column_visibility[key].set(True)
            self.status.configure(text="Keep at least one column visible.", fg=self.colors["error"])
            return
        self.configure_visible_columns()
        if key == "structure" and self.column_visibility[key].get() and not self.inventory_loading:
            self.load_structure_images(self.rows)
        # Tk needs one idle cycle to lay out the newly displayed columns.
        self.after_idle(self.adjust_displayed_column_widths)
        self.profile_settings["visible_columns"] = list(self.visible_column_keys())
        save_profile_settings(self.profile_settings)
    def header_column_key(self, event):
        """Return the displayed column below a pointer in the heading row."""
        if self.tree.identify_region(event.x, event.y) != "heading":
            return None
        return self.displayed_column_key(event)
    def begin_column_drag(self, event):
        self.resizing_column = self.tree.identify_region(event.x, event.y) == "separator"
        self.dragged_column = self.header_column_key(event)
        self.drag_target_column = self.dragged_column
        self.column_dragging = False
        self.column_drag_start = (event.x, event.y)
        return "break" if self.dragged_column else None
    def move_column_drag(self, event):
        if not getattr(self, "dragged_column", None):
            return None
        start_x, start_y = self.column_drag_start
        if max(abs(event.x - start_x), abs(event.y - start_y)) >= 5:
            self.column_dragging = True
        target = self.header_column_key(event)
        if target:
            self.drag_target_column = target
            self.column_dragging = self.column_dragging or target != self.dragged_column
            if not getattr(self, "column_drag_feedback", None):
                self.column_drag_feedback = ColumnDragFeedback(self.tree, self.colors["accent"])
            self.column_drag_feedback.show(event)
        return "break"
    def finish_column_drag(self, event):
        if getattr(self, "resizing_column", False):
            self.resizing_column = False
            self.after_idle(self.remember_column_widths)
            return None
        source = getattr(self, "dragged_column", None)
        target = self.header_column_key(event) or getattr(self, "drag_target_column", None)
        dragged = getattr(self, "column_dragging", False)
        start = getattr(self, "column_drag_start", None)
        if start and max(abs(event.x - start[0]), abs(event.y - start[1])) >= 5:
            dragged = True
        self.dragged_column = None
        self.drag_target_column = None
        self.column_dragging = False
        self.column_drag_start = None
        if getattr(self, "column_drag_feedback", None):
            self.column_drag_feedback.hide()
        if not source:
            return None
        if not dragged:
            if self.header_column_key(event) == source:
                self.sort_by(source)
            return "break"
        if not target or target == source:
            return "break"
        order = list(self.column_order)
        order.remove(source)
        order.insert(order.index(target), source)
        self.column_order = tuple(order)
        self.configure_visible_columns()
        self.profile_settings["column_order"] = list(self.column_order)
        self.profile_settings["visible_columns"] = list(self.visible_column_keys())
        save_profile_settings(self.profile_settings)
        self.after_idle(self.adjust_displayed_column_widths)
        return "break"
    def adjust_displayed_column_widths(self):
        """Fit the currently visible columns to the table, preserving their proportions."""
        if self.profile_settings.get("column_widths"):
            self.restore_column_widths()
            return
        visible = self.displayed_data_column_keys()
        available_width = self.tree.winfo_width() - 2
        if self.column_visibility["structure"].get():
            available_width -= self.tree.column("#0", "width")
        if not visible or available_width <= 1:
            return
        preferred_width = sum(self.column_widths[key] for key in visible)
        target_width = max(available_width, sum(self.column_min_widths[key] for key in visible))
        remaining_width = target_width
        for index, key in enumerate(visible[:-1]):
            remaining_minimum = sum(self.column_min_widths[next_key] for next_key in visible[index + 1:])
            width = max(self.column_min_widths[key], round(target_width * self.column_widths[key] / preferred_width))
            width = min(width, remaining_width - remaining_minimum)
            self.tree.column(key, width=width)
            remaining_width -= width
        self.tree.column(visible[-1], width=remaining_width)
    def displayed_column_key(self, event):
        """Resolve a click to a column key even when columns are hidden."""
        column = self.tree.identify_column(event.x)
        if column == "#0":
            return None
        try: return self.displayed_data_column_keys()[int(column[1:]) - 1]
        except (IndexError, ValueError): return None
    def auto_size_column(self, event):
        """Fit a column to its heading and currently displayed values."""
        if self.tree.identify_region(event.x, event.y) != "separator":
            return None
        column = self.tree.identify_column(event.x)
        key = "structure" if column == "#0" else self.displayed_column_key(event)
        if key is None:
            return None
        font_name = self.style.lookup("Inventory.Treeview", "font") or "TkDefaultFont"
        heading_font_name = self.style.lookup("Inventory.Treeview.Heading", "font") or font_name
        font = tkfont.Font(font=font_name)
        heading_font = tkfont.Font(font=heading_font_name)
        heading = self.tree.heading(column, "text")
        content_width = max([heading_font.measure(heading), *(
            font.measure(self.tree.set(item_id, key))
            for item_id in self.tree.get_children("")
        )])
        width = max(self.column_min_widths[key], content_width + 24)
        self.tree.column("#0" if key == "structure" else key, width=width)
        self.remember_column_widths()
        return "break"
    def remember_credentials_changed(self):
        """Immediately remove saved credentials when Remember is turned off."""
        if self.remember_credentials.get():
            return
        try:
            clear_credentials()
        except Exception as error:
            # Do not leave the control unchecked when secure storage could not be cleared.
            self.remember_credentials.set(True)
            messagebox.showerror(APP_NAME, str(error))

    def login(self):
        username, password = self.username_value.get().strip(), self.password_value.get()
        try: server = normalized_chemotion_url(self.server_value.get())
        except ValueError as error:
            self.status.configure(text=str(error), fg=self.colors["error"])
            return
        if not username or not password:
            self.status.configure(text="Enter both a Chemotion username and password.", fg=self.colors["error"])
            return
        try:
            if self.remember_credentials.get():
                save_credentials(username, password)
            else:
                # Also clear here in case the checkbox was changed programmatically.
                clear_credentials()
        except Exception as error:
            messagebox.showerror(APP_NAME, str(error)); return
        client.CHEMOTION_URL, client.CHEMOTION_USERNAME, client.CHEMOTION_PASSWORD = server, username, password
        self.server_value.set(server)
        self.profile_settings["chemotion_server"] = server
        save_profile_settings(self.profile_settings)
        self.logged_in = False; self.login_button.configure(text="Sign in")
        self.collection_value.set("Choose collection…"); self.collection_choices = {}; self.collection_menu.configure(values=())
        self.load_collections()
    def login_from_return(self, _event=None):
        if not self.logged_in: self.login()
        return "break"
    def connection_details_changed(self, *_args):
        if self.logged_in:
            self.logged_in = False
            self.login_button.configure(text="Sign in", state="normal")
    def load_collections(self):
        self.set_controls_busy(True); self.status.configure(text="Retrieving editable Chemotion collections…",fg=self.colors["accent"]); threading.Thread(target=self._retrieve_collections, daemon=True).start()
    def _retrieve_collections(self):
        try:
            client = ChemotionClient(); client.login(); self.after(0, self._collections_loaded, client.collections())
        except Exception as error: self.after(0, self._collections_failed, str(error))
    def _collections_loaded(self, collections):
        collections = [item for item in collections if str(item.get("label") or "").casefold() != "chemotion-repository.net"]
        # Keep synchronized collections in their own group.  Within that
        # group, organize collections by the displayed source/user code first
        # so collections shared by the same account stay together.
        collections.sort(key=lambda item: (
            bool(item.get("_synchronized")),
            synchronized_collection_source(item).casefold() if item.get("_synchronized") and synchronized_collection_source(item) else "",
            shown(item.get("label")).casefold(),
        ))
        self.collection_choices = {collection_picker_label(item): item for item in collections}
        self.collection_menu.configure(values=tuple(self.collection_choices))
        self.logged_in = True; self.login_button.configure(text="Signed in")
        self.set_controls_busy(False)
        saved = self.profile_settings.get("last_collection")
        selected_label = next((label for label, item in self.collection_choices.items()
                               if isinstance(saved, dict) and item.get("id") == saved.get("id")
                               and bool(item.get("_synchronized")) == bool(saved.get("synchronized"))), None)
        if selected_label:
            self.collection_value.set(selected_label)
            self.status.configure(text="Opening the last used collection…", fg=self.colors["accent"])
            self.after_idle(self.refresh)
        else:
            self.status.configure(text="Choose a collection to retrieve its inventory.", fg=self.colors["muted"])
    def _collections_failed(self, error):
        self.set_controls_busy(False); self.status.configure(text="Chemotion collections could not be retrieved.",fg=self.colors["error"]); messagebox.showerror(APP_NAME, error)
    def export_collection(self):
        collection = self.collection_choices.get(self.collection_value.get())
        if not collection or collection.get("_synchronized"):
            return
        filename = datetime.now().strftime("%y%m%d") + ".zip"
        destination = filedialog.asksaveasfilename(parent=self, title="Export collection", initialfile=filename, defaultextension=".zip", filetypes=[("ZIP archive", "*.zip")])
        if not destination:
            return
        threading.Thread(target=self._export_collection, args=(dict(collection), destination), daemon=True).start()

    def _export_collection(self, collection, destination):
        error = None
        queued = False
        def export_queued():
            nonlocal queued
            queued = True
            self.after(0, self._export_queued)
        try:
            client = ChemotionClient()
            client.login()
            client.export_collection(collection, destination, queued=export_queued)
        except Exception as exception:
            error = str(exception)
        self.after(0, self._collection_exported, destination, error, queued)

    def _export_queued(self):
        self.queued_exports += 1
        self.export_status.configure(text="Export queued")

    def _collection_exported(self, destination, error, queued=False):
        if queued:
            self.queued_exports -= 1
        self.export_status.configure(text="Export queued" if self.queued_exports else "")
        if error:
            messagebox.showerror("Collection export", error)
        else:
            messagebox.showinfo("Collection export", f"Download completed.\n\n{destination}")

    def load_selected_collection(self, _event=None): self.refresh()
    def refresh(self):
        if self.collection_value.get() not in self.collection_choices:
            self.status.configure(text="Choose a collection before retrieving inventory.", fg=self.colors["error"])
            return
        collection = self.collection_choices[self.collection_value.get()]
        self.profile_settings["last_collection"] = {"id": collection["id"], "synchronized": bool(collection.get("_synchronized"))}
        save_profile_settings(self.profile_settings)
        self.rows = []; self.structure_rows_pending.clear(); self.selected_row_ids.clear(); self.location_filter_retained_row_ids.clear(); self.render_rows()
        self.sort_key = "label"; self.sort_reverse = True; self.update_sort_headings()
        cancelled = threading.Event(); self.inventory_cancel_event = cancelled; self.inventory_loading = True
        self.set_controls_busy(True); self.show_progress(); self.status.configure(text="Retrieving the current inventory from Chemotion… (Esc to stop)",fg=self.colors["accent"]); threading.Thread(target=self._retrieve, args=(collection, cancelled, self.column_visibility["structure"].get()), daemon=True).start()
    def cancel_inventory_load(self, _event=None):
        if not self.inventory_loading or not self.inventory_cancel_event or self.inventory_cancel_event.is_set():
            return
        self.inventory_cancel_event.set()
        self.cancel_load_button.configure(state="disabled")
        self.status.configure(text="Stopping inventory loading…", fg=self.colors["muted"])
        return "break"
    def _retrieve(self, collection, cancelled, with_structures=False):
        try:
            def deliver_rows(rows):
                if not with_structures:
                    self.after(0, self._rows_loaded, rows, cancelled)
                    return
                displayed = threading.Event()
                self.after(0, self._rows_loaded, rows, cancelled, displayed)
                # Only the GUI thread can confirm that the image has reached
                # the table. Stop must also release this wait immediately.
                while not displayed.wait(0.1):
                    if cancelled.is_set():
                        return
            rows = ChemotionClient().inventory(collection["id"], collection.get("_synchronized", False),
                                                lambda message: self.after(0, self._progress, message, cancelled), cancelled,
                                                deliver_rows, sequential=with_structures)
            self.after(0, self._loaded, rows, cancelled)
        except InventoryLoadCancelled as stopped:
            self.after(0, self._cancelled, cancelled, stopped.rows)
        except Exception as error:
            if not cancelled.is_set(): self.after(0, self._failed, str(error), cancelled)
    def show_progress(self):
        self.progress.configure(mode="determinate", maximum=100, value=0)
    def _rows_loaded(self, rows, cancelled, displayed=None):
        if cancelled is not self.inventory_cancel_event or not self.inventory_loading:
            if displayed is not None:
                displayed.set()
            return
        self.rows.extend(rows)
        if self.column_visibility["structure"].get():
            self.structure_rows_pending.update(
                row["id"] for row in rows
                if row.get("structure") and row["structure"] not in self.structure_images
            )
        self.update_location_filter()
        self.render_rows()
        if displayed is not None:
            source = rows[0].get("structure") if rows else None
            if (self.column_visibility["structure"].get() and source
                    and source not in self.structure_images):
                self.structure_image_waiters.setdefault(source, []).append(displayed)
            else:
                self.after_idle(displayed.set)
        if self.column_visibility["structure"].get():
            self.load_structure_images(rows)
    def hide_progress(self):
        self.progress.configure(maximum=100, value=0)
    def _progress(self, message, cancelled=None):
        if cancelled is not None and (cancelled.is_set() or cancelled is not self.inventory_cancel_event): return
        self.status.configure(text=f"{message} (Esc to stop)", fg=self.colors["accent"])
        match = re.search(r": (\d+) of (\d+)", message)
        if match and not message.startswith("Retrieving sample list:"):
            current, total = map(int, match.groups())
            self.progress.configure(maximum=max(total, 1), value=current)
    def _loaded(self, rows, cancelled=None):
        if cancelled is not None and cancelled is not self.inventory_cancel_event: return
        self.inventory_loading = False
        self.rows=rows; self.location_filter_retained_row_ids.clear(); self.update_location_filter(); self.render_rows(); self.hide_progress(); self.status.configure(text=f"{len(self.filtered_rows())} samples",fg=self.colors["muted"]); self.set_controls_busy(False); self.print_button.configure(state="normal" if rows else "disabled")
        if self.column_visibility["structure"].get():
            self.load_structure_images(self.filtered_rows())
    def _cancelled(self, cancelled, rows=None):
        if cancelled is self.inventory_cancel_event:
            # Include completed batches already rendered in the table. A Tk
            # callback for the latest batch may still be queued when Stop is
            # handled, even though the worker has already raised cancellation.
            retained_rows = list(rows or [])
            retained_ids = {row.get("id") for row in retained_rows}
            retained_rows.extend(row for row in self.rows if row.get("id") not in retained_ids)
            self._loaded(retained_rows, cancelled)
    def _failed(self,error, cancelled=None):
        if cancelled is not None and (cancelled.is_set() or cancelled is not self.inventory_cancel_event): return
        self.inventory_loading = False
        self.hide_progress(); self.status.configure(text="The Chemotion inventory could not be retrieved.",fg=self.colors["error"]); self.set_controls_busy(False); messagebox.showerror(APP_NAME,error)
    def set_controls_busy(self, busy):
        collection = self.collection_choices.get(self.collection_value.get())
        self.export_button.configure(state="normal" if not busy and self.logged_in and collection and not collection.get("_synchronized") else "disabled")
        self.refresh_button.configure(state="disabled" if busy else "normal")
        self.cancel_load_button.configure(state="normal" if busy and self.inventory_loading else "disabled")
        self.login_button.configure(state="disabled" if busy or self.logged_in else "normal")
        self.server_entry.configure(state="disabled" if busy else "normal")
        self.username_entry.configure(state="disabled" if busy else "normal")
        self.password_entry.configure(state="disabled" if busy else "normal")
        self.remember_checkbutton.configure(state="disabled" if busy else "normal")
        self.collection_menu.configure(state="disabled" if busy else ("readonly" if self.collection_choices else "disabled"))
        self.location_filter_menu.configure(state="disabled" if busy else "readonly")
    def update_sort_headings(self):
        for heading, key in zip(self.headings, self.keys):
            arrow = " ▼" if self.sort_reverse else " ▲"
            self.tree.heading(key, text=f"{heading}{arrow}" if key == self.sort_key else heading)
    def sort_by(self,key): self.sort_reverse = not self.sort_reverse if self.sort_key == key else False; self.sort_key = key; self.update_sort_headings(); self.render_rows()
    def update_location_filter(self):
        locations = location_choices(self.rows)
        choices = ("All locations", *locations); self.location_filter_menu.configure(values=choices)
        if self.location_filter.get() not in choices: self.location_filter.set("All locations")
    def filter_by_location(self, _event=None):
        self.location_filter_retained_row_ids.clear()
        visible_ids = {str(row["id"]) for row in self.filtered_rows()}
        selected_ids = set(self.tree.selection()) & visible_ids
        self.tree.selection_set(tuple(selected_ids))
        self.selected_row_ids = selected_ids
        self.render_rows(); count = len(self.filtered_rows())
        if self.column_visibility["structure"].get() and not self.inventory_loading:
            self.load_structure_images(self.filtered_rows())
        self.status.configure(text=f"{count} samples", fg=self.colors["muted"])
    def filtered_rows(self):
        return rows_for_location_filter(self.rows, self.location_filter.get(), self.location_filter_retained_row_ids)
    def render_rows(self, preserve_display_order=False):
        existing_ids = self.tree.get_children()
        displayed_ids = existing_ids if preserve_display_order else ()
        key = self.sort_key
        def sort_value(item):
            if key == "label":
                return natural_sort_value(item[key])
            if key in {"last_modified", "created"}:
                return last_modified_sort_value(item.get(f"{key}_at"))
            return str(item[key]).casefold()
        rows = self.filtered_rows()
        if self.column_visibility["structure"].get():
            rows = [row for row in rows if row["id"] not in self.structure_rows_pending]
        if preserve_display_order:
            # An edit can change the active sort field (or Chemotion's last
            # modified timestamp).  Keep the user's current visual order
            # until they deliberately choose a column heading again.
            rows_by_id = {str(row["id"]): row for row in rows}
            ordered_rows = [rows_by_id.pop(item_id) for item_id in displayed_ids if item_id in rows_by_id]
            ordered_rows.extend(rows_by_id.values())
        else:
            ordered_rows = sorted(rows, key=sort_value, reverse=self.sort_reverse)
        visible_ids = {str(row["id"]) for row in ordered_rows}
        editing_cell = getattr(self, "editing_cell", None)
        if editing_cell and editing_cell[0] not in visible_ids:
            self.cancel_amount_edit()
        for item_id in existing_ids:
            if item_id not in visible_ids:
                self.tree.delete(item_id)
        existing_ids = set(existing_ids)
        for index, row in enumerate(ordered_rows):
            options = {"values": self.row_values(row), "tags": ("alternate",) if index % 2 else ()}
            image = self.structure_images.get(row.get("structure"))
            if image is not None:
                options["image"] = image
            item_id = str(row["id"])
            if item_id in existing_ids:
                options.setdefault("image", "")
                self.tree.item(item_id, **options)
                self.tree.move(item_id, "", index)
            else:
                self.tree.insert("", index, iid=item_id, **options)
        if editing_cell and editing_cell[0] in visible_ids:
            self.reposition_active_editor()
    def row_values(self, row):
        checkbox = lambda value: "☑" if value else "☐"
        # Treeview fonts apply to an entire row, not an individual column.  Keep
        # inventory numbers in the native table font instead of substituting
        # Unicode mathematical "bold" digits, which macOS renders in another face.
        return (str(row["label"]), row["location"], displayed_row_value(row, "structure"), row["molecule"], displayed_row_value(row, "density"), row["sample"], row["vendor"], row["amount"], displayed_row_value(row, "last_modified"), displayed_row_value(row, "created"), checkbox(row["decoupled"]), checkbox(row["inventory_sample"]))
    def load_structure_images(self, rows):
        """Queue missing structures and fetch them serially as rows appear."""
        with self.structure_image_queue_lock:
            for row in rows:
                source = row.get("structure")
                if (source and source not in self.structure_images
                        and source not in self.structure_image_pending):
                    self.structure_image_pending.add(source)
                    self.structure_image_queue.append(source)
            if not self.structure_image_queue or self.structure_image_worker_active:
                return
            self.structure_image_worker_active = True
        threading.Thread(target=self._load_structure_images, daemon=True).start()
    def _load_structure_images(self):
        client = None
        try:
            import resvg_py
        except Exception as error:
            with self.structure_image_queue_lock:
                failed_sources = tuple(self.structure_image_queue)
                self.structure_image_queue.clear()
                self.structure_image_worker_active = False
            message = f"SVG renderer could not load ({error}). Restart using the application launcher to set up dependencies."
            self.after(0, self._structure_image_dependency_missing, failed_sources, message)
            return
        while True:
            with self.structure_image_queue_lock:
                if not self.structure_image_queue:
                    self.structure_image_worker_active = False
                    return
                source = self.structure_image_queue.popleft()
            try:
                if isinstance(source, str) and "<svg" in source[:4096].lower():
                    svg_data = source.encode("utf-8")
                else:
                    if client is None:
                        client = ChemotionClient()
                        client.login()
                    path = structure_image_request_path(source)
                    svg_data = client.request(path)
                if b"<svg" not in svg_data[:4096].lower():
                    raise ValueError("Chemotion did not return an SVG structure image.")
                dimensions = re.search(rb'\bviewBox=["\']\s*([\d.+-]+)[ ,]+([\d.+-]+)[ ,]+([\d.+-]+)[ ,]+([\d.+-]+)', svg_data[:4096], re.IGNORECASE)
                if dimensions:
                    _x, _y, width, height = (float(value) for value in dimensions.groups())
                    aspect_ratio = width / height if width > 0 and height > 0 else 1.0
                else:
                    width = re.search(rb'\bwidth=["\']\s*([\d.]+)', svg_data[:4096], re.IGNORECASE)
                    height = re.search(rb'\bheight=["\']\s*([\d.]+)', svg_data[:4096], re.IGNORECASE)
                    width_value = float(width.group(1)) if width else 1.0
                    height_value = float(height.group(1)) if height else 1.0
                    aspect_ratio = width_value / height_value if height_value > 0 else 1.0
                size = self.structure_size
                if aspect_ratio >= 75 / 50:
                    output_width, output_height = round(size * 1.5), max(1, round(size * 1.5 / aspect_ratio))
                else:
                    output_width, output_height = max(1, round(size * aspect_ratio)), size
                image_data = resvg_py.svg_to_bytes(svg_string=svg_data.decode("utf-8-sig"),
                                                  width=output_width, height=output_height)
                dark_image_data = inverted_structure_png(image_data, output_width, output_height)
            except Exception as error:
                self.after(0, self._structure_image_failed, source, str(error))
            else:
                self.after(0, self._structure_image_loaded, source, image_data, dark_image_data,
                           svg_data, size)
    def _structure_image_dependency_missing(self, sources, error):
        self.structure_image_pending.difference_update(sources)
        self.status.configure(
            text=f"Structure thumbnail rendering unavailable: {error}",
            fg=self.colors["error"],
        )
        for source in sources:
            self._structure_image_finished(source)
    def _structure_image_loaded(self, source, image_data, dark_image_data=None, svg_data=None, rendered_size=None):
        self.structure_image_pending.discard(source)
        try:
            if svg_data is not None:
                self.structure_svg_data[source] = svg_data
            if source in getattr(self, "structure_svg_data", {}) and rendered_size != self.structure_size:
                import resvg_py
                svg = self.structure_svg_data[source].decode("utf-8-sig")
                # Re-render the vector source so enlarging a thumbnail stays sharp.
                image_data = resvg_py.svg_to_bytes(svg_string=svg,
                                                   width=round(self.structure_size * 1.5),
                                                   height=self.structure_size)
                dark_image_data = inverted_structure_png(image_data,
                                                          round(self.structure_size * 1.5),
                                                          self.structure_size)
            image = tk.PhotoImage(data=base64.b64encode(image_data).decode("ascii"), format="png")
            if dark_image_data is not None:
                dark_image = tk.PhotoImage(data=base64.b64encode(dark_image_data).decode("ascii"), format="png")
                self.structure_image_variants[source] = (image, dark_image)
                if self.dark_mode:
                    image = dark_image
            self.structure_images[source] = image
            for row in self.rows:
                if row.get("structure") == source and self.tree.exists(str(row["id"])):
                    self.tree.item(str(row["id"]), image=image)
        except Exception as error:
            self._structure_image_failed(source, str(error))
        else:
            self._structure_image_finished(source)
    def _structure_image_failed(self, source, error):
        self.structure_image_pending.discard(source)
        message = " ".join(str(error).split())
        if len(message) > 120:
            message = message[:117] + "…"
        self.status.configure(text=f"Structure thumbnail failed: {message}", fg=self.colors["muted"])
        self._structure_image_finished(source)
    def _structure_image_finished(self, source):
        ready_ids = {row["id"] for row in self.rows if row.get("structure") == source}
        if self.structure_rows_pending.intersection(ready_ids):
            self.structure_rows_pending.difference_update(ready_ids)
            self.render_rows()
        for displayed in self.structure_image_waiters.pop(source, []):
            self.after_idle(displayed.set)
    def remember_selection(self, _event=None):
        self.selected_row_ids = set(self.tree.selection())
    def select_all_visible(self, _event=None):
        self.tree.selection_set(self.tree.get_children())
        self.selected_row_ids = set(self.tree.selection())
        return "break"
    def delete_selected_row(self, _event=None):
        """Confirm removal of the focused selected row from this collection."""
        selected_ids = set(self.tree.selection())
        item_id = self.tree.focus()
        if item_id not in selected_ids:
            item_id = next(iter(selected_ids), None)
        row = next((item for item in self.rows if str(item["id"]) == item_id), None)
        if not row:
            return "break"
        collection = self.collection_choices.get(self.collection_value.get())
        if not collection:
            self.status.configure(text="Choose a collection before removing a sample.", fg=self.colors["error"])
            return "break"
        sample = row["sample"] or f"sample {row['label']}"
        short_label = row["label"] or sample
        if not messagebox.askyesno(
                "Remove from collection?",
                f"Remove {sample} from the selected collection?\n\nIt will remain in Chemotion and any other collections.",
                parent=self):
            return "break"
        self.cancel_amount_edit()
        self.set_controls_busy(True); self.print_button.configure(state="disabled"); self.show_progress()
        self.status.configure(text=f"Removing {short_label} from the collection…", fg=self.colors["accent"])
        threading.Thread(target=self._remove_sample_from_collection,
                         args=(row, collection["id"], bool(collection.get("_synchronized")),
                               collection.get("_source_collection_id")), daemon=True).start()
        return "break"
    def _remove_sample_from_collection(self, row, collection_id, synchronized=False, source_collection_id=None):
        try:
            client = ChemotionClient(); client.login()
            result = client.remove_sample_from_collection(collection_id, row["id"], synchronized, source_collection_id)
            self.after(0, self._sample_removed_from_collection, row, result.get("locked_sample_ids") or [])
        except Exception as error:
            self.after(0, self._sample_removal_failed, str(error))
    def _sample_removed_from_collection(self, row, locked_sample_ids):
        if row["id"] in locked_sample_ids or str(row["id"]) in {str(item) for item in locked_sample_ids}:
            self.hide_progress(); self.set_controls_busy(False)
            self.print_button.configure(state="normal" if self.rows else "disabled")
            self.status.configure(text=f"{row['label']} remains in this collection because it is linked to another item.", fg=self.colors["error"])
            return
        self.rows = [item for item in self.rows if item is not row]
        self.selected_row_ids.clear(); self.update_location_filter(); self.render_rows(preserve_display_order=True)
        self.hide_progress(); self.set_controls_busy(False)
        self.print_button.configure(state="normal" if self.rows else "disabled")
        self.status.configure(text=f"Removed {row['label']} from the selected collection.", fg=self.colors["muted"])
    def _sample_removal_failed(self, error):
        self.hide_progress(); self.set_controls_busy(False)
        self.print_button.configure(state="normal" if self.rows else "disabled")
        self.status.configure(text="The sample could not be removed from the collection.", fg=self.colors["error"])
        messagebox.showerror(APP_NAME, error)
    def extend_edit_selection(self, event):
        """Extend row selection without opening an editor on a modifier-click."""
        item_id = self.tree.identify_row(event.y)
        if not item_id: return "break"
        selected_ids = set(self.tree.selection())
        if event.state & 0x0001:  # Shift: add the visible range from the focus row.
            items = self.tree.get_children()
            anchor = self.tree.focus()
            if anchor in items:
                start, end = sorted((items.index(anchor), items.index(item_id)))
                selected_ids.update(items[start:end + 1])
            else:
                selected_ids.add(item_id)
        else:  # Command: add this row while preserving the existing selection.
            selected_ids.add(item_id)
        self.tree.selection_set(tuple(selected_ids)); self.tree.focus(item_id)
        self.selected_row_ids = selected_ids
        return "break"
    def edit_amount(self, event):
        """Edit the supported inventory fields directly in the table."""
        item_id = self.tree.identify_row(event.y)
        if not item_id: return
        row = next((item for item in self.rows if str(item["id"]) == item_id), None)
        if not row: return
        column = self.displayed_column_key(event)
        if column not in {"location", "molecule", "density", "sample", "vendor", "amount", "decoupled", "inventory_sample"}: return
        # Starting an edit on an already selected row must not collapse a
        # multi-selection: the entered value is applied to that selection.
        selected_ids = self.selected_row_ids or set(self.tree.selection())
        if item_id not in selected_ids:
            selected_ids = {item_id}
            self.tree.selection_set(item_id)
        self.tree.focus(item_id)
        self.selected_row_ids = selected_ids
        self.editing_rows = [candidate for candidate in self.filtered_rows() if str(candidate["id"]) in selected_ids]
        if column == "location": return self.edit_location(row, item_id)
        if column == "molecule": return self.edit_name(row, item_id, "molecule")
        if column == "density": return self.edit_name(row, item_id, "density")
        if column == "sample": return self.edit_name(row, item_id, "sample")
        if column == "vendor": return self.edit_vendor(row, item_id)
        if column == "decoupled": return self.change_selected_flags(item_id, "decoupled")
        if column == "inventory_sample": return self.change_selected_flags(item_id, "inventory_sample")
        if column != "amount": return
        self.cancel_amount_edit()
        self.editing_rows = self.selected_edit_rows(row)
        box = self.tree.bbox(item_id, "amount")
        if not box: return "break"
        x, y, width, height = box
        editor = ttk.Entry(self.tree)
        editor.insert(0, row["amount"] if row["amount"] not in {"", "—"} else "")
        self.place_editor(editor, item_id, "amount")
        self.amount_editor = editor
        editor.focus_set(); editor.selection_range(0, "end")
        editor.bind("<Return>", lambda _event: self.finish_amount_edit(row, advance=True))
        editor.bind("<Escape>", lambda _event: self.cancel_amount_edit())
        self.bind_editor_navigation(editor, item_id)
        editor.bind("<FocusOut>", lambda _event: self.finish_amount_edit(row))
        return "break"
    def edit_vendor(self, row, item_id):
        self.cancel_amount_edit()
        self.editing_rows = self.selected_edit_rows(row)
        box = self.tree.bbox(item_id, "vendor")
        if not box: return "break"
        x, y, width, height = box
        editor = ttk.Entry(self.tree); editor.insert(0, "" if row["vendor"] == "—" else row["vendor"])
        self.place_editor(editor, item_id, "vendor"); self.amount_editor = editor
        editor.focus_set(); editor.selection_range(0, "end")
        editor.bind("<Return>", lambda _event: self.finish_vendor_edit(row, advance=True))
        editor.bind("<Escape>", lambda _event: self.cancel_amount_edit())
        self.bind_editor_navigation(editor, item_id)
        editor.bind("<FocusOut>", lambda _event: self.finish_vendor_edit(row))
        return "break"
    def edit_location(self, row, item_id):
        self.cancel_amount_edit()
        self.editing_rows = self.selected_edit_rows(row)
        box = self.tree.bbox(item_id, "location")
        if not box: return "break"
        # Select a known location or type a new one directly in the cell.
        editor = ttk.Combobox(self.tree, values=location_choices(self.rows), state="normal")
        editor.set(row["location"])
        self.place_editor(editor, item_id, "location"); self.amount_editor = editor
        editor.focus_set(); editor.selection_range(0, "end")
        editor.bind("<Return>", lambda _event: self.finish_location_edit(row, advance=True))
        editor.bind("<<ComboboxSelected>>", lambda _event: self.finish_location_edit(row))
        editor.bind("<Escape>", lambda _event: self.cancel_amount_edit())
        self.bind_editor_navigation(editor, item_id)
        editor.bind("<FocusOut>", lambda _event: self.finish_location_edit(row))
        return "break"
    def edit_amount_cell(self, row, item_id):
        self.cancel_amount_edit()
        self.editing_rows = self.selected_edit_rows(row)
        box = self.tree.bbox(item_id, "amount")
        if not box: return "break"
        x, y, width, height = box
        editor = ttk.Entry(self.tree)
        editor.insert(0, row["amount"] if row["amount"] not in {"", "—"} else "")
        self.place_editor(editor, item_id, "amount")
        self.amount_editor = editor
        editor.focus_set(); editor.selection_range(0, "end")
        editor.bind("<Return>", lambda _event: self.finish_amount_edit(row, advance=True))
        editor.bind("<Escape>", lambda _event: self.cancel_amount_edit())
        self.bind_editor_navigation(editor, item_id)
        editor.bind("<FocusOut>", lambda _event: self.finish_amount_edit(row))
        return "break"
    def edit_name(self, row, item_id, field):
        self.cancel_amount_edit()
        self.editing_rows = self.selected_edit_rows(row)
        box = self.tree.bbox(item_id, field)
        if not box: return "break"
        x, y, width, height = box
        editor = ttk.Entry(self.tree); editor.insert(0, "" if row[field] == "—" else row[field])
        self.place_editor(editor, item_id, field); self.amount_editor = editor
        editor.focus_set(); editor.selection_range(0, "end")
        editor.bind("<Return>", lambda _event: self.finish_name_edit(row, field, advance=True))
        editor.bind("<Escape>", lambda _event: self.cancel_amount_edit())
        self.bind_editor_navigation(editor, item_id)
        editor.bind("<FocusOut>", lambda _event: self.finish_name_edit(row, field))
        return "break"
    def finish_name_edit(self, row, field, advance=False):
        editor = self.amount_editor
        if not editor: return "break"
        value = editor.get().strip()
        if field == "density" and value:
            try:
                number = Decimal(value.replace(",", "."))
                if not number.is_finite() or number < 0: raise InvalidOperation
            except InvalidOperation:
                self.status.configure(text="Enter a non-negative density or leave the cell empty.", fg=self.colors["error"])
                editor.focus_set(); editor.selection_range(0, "end")
                return "break"
            value = str(number)
        if not value and field == "sample":
            self.status.configure(text=f"{('Molecule' if field == 'molecule' else 'Sample')} name cannot be empty.", fg=self.colors["error"])
            editor.focus_set(); editor.selection_range(0, "end")
            return "break"
        rows = self.editing_rows or [row]
        if value == row[field] and len(rows) == 1:
            if advance: self.queue_next_edit(str(row["id"]), field, advance)
            self.cancel_amount_edit(); self.edit_saved_next_cell(); return "break"
        if advance and len(rows) == 1: self.queue_next_edit(str(row["id"]), field, advance)
        self.cancel_amount_edit()
        if field == "molecule": self._save_molecule_name(rows, value)
        elif field == "density": self._start_sample_edit(rows, "density", self._change_density, value or None)
        else: self._save_sample_name(rows, value)
        return "break"
    def finish_location_edit(self, row, advance=False):
        editor = self.amount_editor
        if not editor: return "break"
        location = editor.get().strip()
        if not location:
            self.status.configure(text="Location cannot be empty.", fg=self.colors["error"])
            editor.focus_set(); editor.selection_range(0, "end")
            return "break"
        rows = self.editing_rows or [row]
        if location == row["location"] and len(rows) == 1:
            if advance: self.queue_next_edit(str(row["id"]), "location", advance)
            self.cancel_amount_edit(); self.edit_saved_next_cell(); return "break"
        if advance and len(rows) == 1: self.queue_next_edit(str(row["id"]), "location", advance)
        self.cancel_amount_edit()
        self.set_controls_busy(True); self.print_button.configure(state="disabled"); self.show_progress()
        self.status.configure(text=f"Changing locations for {len(rows)} sample{'s' if len(rows) != 1 else ''}…", fg=self.colors["accent"])
        threading.Thread(target=self._change_locations, args=(rows, location), daemon=True).start()
        return "break"
    def change_selected_flags(self, item_id, field):
        """Toggle one displayed checkbox for all currently selected samples."""
        selected_ids = self.selected_row_ids or set(self.tree.selection())
        if item_id not in selected_ids:
            # A checkbox click should edit its own row when it is not already
            # part of a multi-selection, rather than retaining an unrelated
            # prior selection.
            selected_ids = {item_id}
            self.tree.selection_set(item_id)
        selected_rows = [row for row in self.filtered_rows() if str(row["id"]) in selected_ids]
        if not selected_rows: return "break"
        value = not next(row for row in selected_rows if str(row["id"]) == item_id)[field]
        self.selected_row_ids = {str(row["id"]) for row in selected_rows}
        label = "Decoupled" if field == "decoupled" else "Inventory"
        self.set_controls_busy(True); self.print_button.configure(state="disabled"); self.show_progress()
        self.status.configure(text=f"Updating {label} for {len(selected_rows)} sample{'s' if len(selected_rows) != 1 else ''}…", fg=self.colors["accent"])
        threading.Thread(target=self._change_flags, args=(selected_rows, field, value, label), daemon=True).start()
        return "break"
    def _change_flags(self, selected_rows, field, value, label):
        failures = []
        try:
            client = ChemotionClient(); client.login()
            for position, row in enumerate(selected_rows, start=1):
                self.after(0, self._progress, f"Updating {label}: {position} of {len(selected_rows)}…")
                try:
                    changes = {"decoupled": row["decoupled"], "inventory_sample": row["inventory_sample"]}
                    changes[field] = value
                    client.set_sample_flags(row["id"], row["container"], **changes)
                except Exception as error:
                    failures.append((row, str(error)))
                else:
                    refreshed = client.inventory_entry(row["id"], row.get("collection_id"), row.get("synchronized", False))
                    row.clear(); row.update(refreshed)
            self.after(0, self._flags_changed, selected_rows, field, value, label, failures)
        except Exception as error:
            self.after(0, self._flags_change_failed, label, str(error))
    def _flags_changed(self, selected_rows, field, value, label, failures):
        self.selected_row_ids.clear(); self.render_rows(preserve_display_order=True); self.hide_progress()
        self.set_controls_busy(False); self.print_button.configure(state="normal" if self.rows else "disabled")
        changed = len(selected_rows) - len(failures)
        if failures:
            self.status.configure(text=f"Updated {label} for {changed} of {len(selected_rows)} samples; {len(failures)} could not be changed.", fg=self.colors["error"])
            messagebox.showerror(APP_NAME, "Some samples could not be changed:\n\n" + "\n".join(f"{row['sample']}: {error}" for row, error in failures[:5]))
        else:
            self.status.configure(text=f"Set {label} to {'checked' if value else 'unchecked'} for {changed} sample{'s' if changed != 1 else ''}.", fg=self.colors["muted"])
    def _flags_change_failed(self, label, error):
        self.hide_progress(); self.set_controls_busy(False); self.print_button.configure(state="normal" if self.rows else "disabled")
        self.status.configure(text=f"{label} could not be changed.", fg=self.colors["error"])
        messagebox.showerror(APP_NAME, error)
    def open_sample(self, event):
        """Open a sample in Chemotion when a non-editable cell is double-clicked."""
        item_id = self.tree.identify_row(event.y)
        if not item_id or self.displayed_column_key(event) in {"amount", "decoupled", "inventory_sample"}: return
        row = next((item for item in self.rows if str(item["id"]) == item_id), None)
        if not row: return
        collection_id = row.get("collection_id")
        if not collection_id:
            self.status.configure(text="This sample has no Chemotion collection reference.", fg=self.colors["error"])
            return "break"
        route = "scollection" if row.get("synchronized") else "collection"
        webbrowser.open_new_tab(f"{client.CHEMOTION_URL}/mydb/{route}/{collection_id}/sample/{row['id']}")
        self.set_controls_busy(True); self.print_button.configure(state="disabled"); self.show_progress()
        self.status.configure(text=f"Opened {row['label']} in Chemotion; refreshing sample…", fg=self.colors["accent"])
        threading.Thread(target=self._refresh_opened_sample, args=(row,), daemon=True).start()
        return "break"
    def _refresh_opened_sample(self, row):
        try:
            client = ChemotionClient(); client.login()
            refreshed = client.inventory_entry(row["id"], row.get("collection_id"), row.get("synchronized", False))
            self.after(0, self._entry_refreshed, row, refreshed, f"Refreshed {row['label']} after opening Chemotion.")
        except Exception as error:
            self.after(0, self._opened_sample_refresh_failed, str(error))
    def _opened_sample_refresh_failed(self, error):
        self.hide_progress(); self.set_controls_busy(False)
        self.print_button.configure(state="normal" if self.rows else "disabled")
        self.status.configure(text="Chemotion opened, but the sample information could not be refreshed.", fg=self.colors["error"])
        messagebox.showerror(APP_NAME, error)
    def cancel_amount_edit(self):
        if self.amount_editor:
            editor, self.amount_editor = self.amount_editor, None
            editor.destroy()
        self.editing_cell = None
        self.editing_rows = []
        # Consume Escape before it reaches the window's stop-loading binding.
        return "break"
    def tree_yview_changed(self, first, last):
        self.vertical_scrollbar.set(first, last)
        self.reposition_active_editor()
    def tree_xview_changed(self, first, last):
        self.horizontal_scrollbar.set(first, last)
        self.reposition_active_editor()
    def place_editor(self, editor, item_id, column):
        editor.configure(font=self.sample_list_font)
        self.editing_cell = (item_id, column)
        self.reposition_active_editor(editor)
    def reposition_active_editor(self, editor=None):
        if not self.editing_cell: return
        editor = editor or self.amount_editor
        if not editor or not editor.winfo_exists(): return
        item_id, column = self.editing_cell
        box = self.tree.bbox(item_id, column)
        if not box:
            editor.place_forget()
            return
        x, y, width, height = box
        editor.place(x=x, y=y, width=width, height=height)
    def selected_edit_rows(self, fallback):
        selected_ids = self.selected_row_ids or set(self.tree.selection())
        rows = [candidate for candidate in self.filtered_rows() if str(candidate["id"]) in selected_ids]
        return rows or [fallback]
    def bind_editor_navigation(self, editor, item_id):
        """Bind commit navigation and the existing discard-and-move arrows."""
        editor.bind("<Up>", lambda _event: self.move_editor_focus(item_id, -1))
        editor.bind("<Down>", lambda _event: self.move_editor_focus(item_id, 1))
        editor.bind("<Shift-Return>", lambda _event: self.commit_editor_navigation(item_id, "up"))
        editor.bind("<Tab>", lambda _event: self.commit_editor_navigation(item_id, "right"))
        editor.bind("<Shift-Tab>", lambda _event: self.commit_editor_navigation(item_id, "left"))
        editor.bind("<ISO_Left_Tab>", lambda _event: self.commit_editor_navigation(item_id, "left"))
    def commit_editor_navigation(self, item_id, direction):
        if not self.editing_cell: return "break"
        _, field = self.editing_cell
        row = next((item for item in self.rows if str(item["id"]) == item_id), None)
        if not row: return "break"
        if field == "amount": return self.finish_amount_edit(row, advance=direction)
        if field == "vendor": return self.finish_vendor_edit(row, advance=direction)
        if field == "location": return self.finish_location_edit(row, advance=direction)
        return self.finish_name_edit(row, field, advance=direction)
    def move_editor_focus(self, item_id, direction):
        if not self.editing_cell: return "break"
        _, field = self.editing_cell
        items = self.tree.get_children()
        try: target_index = items.index(item_id) + direction
        except ValueError: return "break"
        if not 0 <= target_index < len(items): return "break"
        target = items[target_index]
        self.cancel_amount_edit()
        # Arrow navigation deliberately discards the value being typed, just
        # like the previous behaviour, but keeps the inline editor open on
        # the corresponding cell in the adjacent visible row.
        self.edit_after_save = (target, field)
        self.edit_saved_next_cell()
        return "break"
    def next_visible_item(self, item_id):
        items = self.tree.get_children()
        try: return items[items.index(item_id) + 1]
        except (ValueError, IndexError): return None
    def queue_next_edit(self, item_id, field, direction=True):
        if direction in {"left", "right"}:
            editable = {"location", "molecule", "density", "sample", "vendor", "amount"}
            columns = [column for column in self.tree["displaycolumns"] if column in editable]
            try: index = columns.index(field) + (1 if direction == "right" else -1)
            except ValueError:
                self.edit_after_save = None
                return
            self.edit_after_save = (item_id, columns[index]) if 0 <= index < len(columns) else None
            return
        if direction == "up":
            items = self.tree.get_children()
            try: index = items.index(item_id) - 1
            except ValueError: index = -1
            self.edit_after_save = (items[index], field) if index >= 0 else None
            return
        next_item = self.next_visible_item(item_id)
        self.edit_after_save = (next_item, field) if next_item else None
    def edit_saved_next_cell(self):
        target = self.edit_after_save; self.edit_after_save = None
        if not target: return
        item_id, field = target
        row = next((item for item in self.rows if str(item["id"]) == item_id), None)
        if not row or not self.tree.exists(item_id): return
        self.tree.see(item_id); self.tree.selection_set(item_id); self.tree.focus(item_id)
        self.selected_row_ids = {item_id}
        if field == "amount": self.edit_amount_cell(row, item_id)
        elif field == "vendor": self.edit_vendor(row, item_id)
        elif field == "location": self.edit_location(row, item_id)
        else: self.edit_name(row, item_id, field)
    def finish_amount_edit(self, row, advance=False):
        editor = self.amount_editor
        if not editor: return "break"
        try: value, unit = parse_amount(editor.get())
        except ValueError as error:
            self.status.configure(text=str(error), fg=self.colors["error"])
            editor.focus_set(); editor.selection_range(0, "end")
            return "break"
        rows = self.editing_rows or [row]
        if advance and len(rows) == 1: self.queue_next_edit(str(row["id"]), "amount", advance)
        self.cancel_amount_edit(); self._save_amount(rows, value, unit)
        return "break"
    def finish_vendor_edit(self, row, advance=False):
        editor = self.amount_editor
        if not editor: return "break"
        vendor = editor.get().strip()
        rows = self.editing_rows or [row]
        if advance and len(rows) == 1: self.queue_next_edit(str(row["id"]), "vendor", advance)
        self.cancel_amount_edit(); self._save_vendor(rows, vendor)
        return "break"
    def _start_sample_edit(self, rows, label, target, *values):
        self.set_controls_busy(True)
        self.print_button.configure(state="disabled")
        self.show_progress()
        self.status.configure(
            text=f"Updating the {label} for {len(rows)} sample{'s' if len(rows) != 1 else ''}…",
            fg=self.colors["accent"],
        )
        threading.Thread(target=target, args=(rows, *values), daemon=True).start()
    def _save_amount(self, rows, value, unit):
        self._start_sample_edit(rows, "amount", self._change_amount, value, unit)
    def _save_sample_name(self, rows, name):
        self._start_sample_edit(rows, "sample name", self._change_sample_name, name)
    def _change_sample_name(self, rows, name):
        failures = []
        try:
            client = ChemotionClient(); client.login()
            for position, row in enumerate(rows, start=1):
                self.after(0, self._progress, f"Updating sample name: {position} of {len(rows)}…")
                try:
                    client.set_sample_name(row["id"], name, row["container"], row["decoupled"], row["inventory_sample"])
                    refresh_inventory_row(client, row)
                except Exception as error: failures.append((row, str(error)))
            self.after(0, self._batch_edit_finished, rows, failures, "sample name")
        except Exception as error:
            self.after(0, self._name_change_failed, "sample name", str(error))
    def _change_density(self, rows, density):
        failures = []
        try:
            client = ChemotionClient(); client.login()
            for position, row in enumerate(rows, start=1):
                self.after(0, self._progress, f"Updating density: {position} of {len(rows)}…")
                try:
                    client.set_density(row["id"], density, row["container"], row["decoupled"], row["inventory_sample"])
                    refresh_inventory_row(client, row)
                except Exception as error: failures.append((row, str(error)))
            self.after(0, self._batch_edit_finished, rows, failures, "density")
        except Exception as error:
            self.after(0, self._name_change_failed, "density", str(error))
    def _save_molecule_name(self, rows, name):
        if any(not row.get("molecule_id") for row in rows):
            self.edit_after_save = None
            self.status.configure(text="One or more selected samples have no Chemotion molecule reference.", fg=self.colors["error"])
            return
        self._start_sample_edit(rows, "molecule name", self._change_molecule_name, name)
    def _change_molecule_name(self, rows, name):
        failures = []
        try:
            client = ChemotionClient(); client.login()
            for position, row in enumerate(rows, start=1):
                self.after(0, self._progress, f"Updating molecule name: {position} of {len(rows)}…")
                try:
                    name_id = client.create_molecule_name(row["molecule_id"], name) if name else None
                    client.set_molecule_name(row["id"], name_id, row["container"], row["decoupled"], row["inventory_sample"])
                    # A molecule-name edit only changes these two values.  Do
                    # not replace the row from a follow-up request: Chemotion
                    # can briefly return an incomplete sample there, which
                    # made the displayed list disappear during the update.
                    apply_molecule_name_update(row, name, name_id)
                except Exception as error: failures.append((row, str(error)))
            self.after(0, self._batch_edit_finished, rows, failures, "molecule name")
        except Exception as error:
            self.after(0, self._name_change_failed, "molecule name", str(error))
    def _name_changed(self, row, field, name, molecule_name_id=None):
        row[field] = shown(name) if field == "molecule" else name
        if molecule_name_id is not None: row["molecule_name_id"] = molecule_name_id
        self.render_rows(preserve_display_order=True); self.hide_progress(); self.set_controls_busy(False)
        self.print_button.configure(state="normal" if self.rows else "disabled")
        self.status.configure(text=f"Updated the {field} name for {row['label']}.", fg=self.colors["muted"])
        self.edit_saved_next_cell()
    def _entry_refreshed(self, row, refreshed, status):
        """Replace an edited row with Chemotion's authoritative current record."""
        selected_ids = set(self.selected_row_ids)
        selected_ids.add(str(row["id"]))
        row.clear(); row.update(refreshed)
        self.render_rows(preserve_display_order=True)
        visible_ids = set(self.tree.get_children())
        selected_ids.intersection_update(visible_ids)
        if selected_ids:
            self.tree.selection_set(tuple(selected_ids))
            self.tree.focus(str(row["id"]))
            self.tree.see(str(row["id"]))
        self.selected_row_ids = selected_ids
        self.hide_progress(); self.set_controls_busy(False)
        self.print_button.configure(state="normal" if self.rows else "disabled")
        self.status.configure(text=status, fg=self.colors["muted"])
        self.edit_saved_next_cell()
    def _name_change_failed(self, label, error):
        self.edit_after_save = None
        self.hide_progress(); self.set_controls_busy(False); self.print_button.configure(state="normal" if self.rows else "disabled")
        self.status.configure(text=f"The {label} could not be changed.", fg=self.colors["error"])
        messagebox.showerror(APP_NAME, error)
    def _change_amount(self, rows, value, unit):
        failures = []
        try:
            client = ChemotionClient(); client.login()
            for position, row in enumerate(rows, start=1):
                self.after(0, self._progress, f"Updating amount: {position} of {len(rows)}…")
                try:
                    field = row["amount_field"] or "real_amount"
                    client.set_amount(row["id"], field, value, unit, row["container"], row["decoupled"], row["inventory_sample"])
                    refresh_inventory_row(client, row, require_amount=True)
                except Exception as error: failures.append((row, str(error)))
            self.after(0, self._batch_edit_finished, rows, failures, "amount")
        except Exception as error:
            self.after(0, self._amount_change_failed, str(error))
    def _amount_changed(self, row, field, value, unit):
        row["amount_field"] = field; row["amount_value"] = value; row["amount_unit"] = unit
        row["amount"] = "" if is_zero_amount(value) else format_amount_with_unit(value, unit)
        self.render_rows(preserve_display_order=True); self.hide_progress(); self.set_controls_busy(False)
        self.print_button.configure(state="normal" if self.rows else "disabled")
        self.status.configure(text=f"Updated the amount for {row['label']} to {row['amount'] or '0 ' + unit}.", fg=self.colors["muted"])
        self.edit_saved_next_cell()
    def _amount_change_failed(self, error):
        self.edit_after_save = None
        self.hide_progress(); self.set_controls_busy(False); self.print_button.configure(state="normal" if self.rows else "disabled")
        self.status.configure(text="The amount could not be changed.", fg=self.colors["error"])
        messagebox.showerror(APP_NAME, error)
    def _save_vendor(self, rows, vendor):
        self._start_sample_edit(rows, "vendor", self._change_vendor, vendor)
    def _change_vendor(self, rows, vendor):
        failures = []
        try:
            client = ChemotionClient(); client.login()
            for position, row in enumerate(rows, start=1):
                self.after(0, self._progress, f"Updating vendor: {position} of {len(rows)}…")
                try:
                    client.set_vendor(row["id"], row.get("chemical_data"), vendor,
                                      row.get("chemical_id"), row.get("chemical_cas", ""))
                    refresh_inventory_row(client, row)
                except Exception as error: failures.append((row, str(error)))
            self.after(0, self._batch_edit_finished, rows, failures, "vendor")
        except Exception as error:
            self.after(0, self._vendor_change_failed, str(error))
    def _batch_edit_finished(self, rows, failures, label):
        self.render_rows(preserve_display_order=True); self.hide_progress(); self.set_controls_busy(False)
        self.print_button.configure(state="normal" if self.rows else "disabled")
        changed = len(rows) - len(failures)
        if self.selected_row_ids: self.tree.selection_set(tuple(self.selected_row_ids & set(self.tree.get_children())))
        if failures:
            self.status.configure(text=f"Updated {label} for {changed} of {len(rows)} samples; {len(failures)} could not be changed.", fg=self.colors["error"])
            messagebox.showerror(APP_NAME, "Some samples could not be changed:\n\n" + "\n".join(f"{row['sample']}: {error}" for row, error in failures[:5]))
        else:
            self.status.configure(text=f"Updated the {label} for {changed} sample{'s' if changed != 1 else ''}.", fg=self.colors["muted"])
        self.edit_saved_next_cell()
    def _vendor_changed(self, row, vendor):
        row["vendor"] = shown(vendor)
        data = row.get("chemical_data") or [{}]
        if not isinstance(data[0], dict): data[0] = {}
        data[0]["vendor"] = vendor; row["chemical_data"] = data
        self.render_rows(preserve_display_order=True); self.hide_progress(); self.set_controls_busy(False)
        self.print_button.configure(state="normal" if self.rows else "disabled")
        self.status.configure(text=f"Updated the vendor for {row['label']}.", fg=self.colors["muted"])
        self.edit_saved_next_cell()
    def _vendor_change_failed(self, error):
        self.edit_after_save = None
        self.hide_progress(); self.set_controls_busy(False); self.print_button.configure(state="normal" if self.rows else "disabled")
        self.status.configure(text="The vendor could not be changed.", fg=self.colors["error"])
        messagebox.showerror(APP_NAME, error)
    def _change_locations(self, selected_rows, location):
        failures = []
        try:
            client = ChemotionClient(); client.login()
            for position, row in enumerate(selected_rows, start=1):
                self.after(0, self._progress, f"Changing locations: {position} of {len(selected_rows)}…")
                try:
                    client.set_location(row["id"], location, row["container"], row["decoupled"], row["inventory_sample"])
                    refresh_inventory_row(client, row)
                except Exception as error:
                    failures.append((row, str(error)))
            self.after(0, self._locations_changed, selected_rows, location, failures)
        except Exception as error:
            self.after(0, self._locations_change_failed, str(error))
    def _locations_changed(self, selected_rows, location, failures):
        failed_row_ids = {str(row["id"]) for row, _error in failures}
        self.location_filter_retained_row_ids.update(
            str(row["id"]) for row in selected_rows if str(row["id"]) not in failed_row_ids
        )
        self.update_location_filter(); self.selected_row_ids.clear()
        self.render_rows(preserve_display_order=True); self.hide_progress()
        self.set_controls_busy(False); self.print_button.configure(state="normal" if self.rows else "disabled")
        changed = len(selected_rows) - len(failures)
        if failures:
            self.status.configure(text=f"Changed {changed} of {len(selected_rows)} locations; {len(failures)} could not be changed.", fg=self.colors["error"])
            messagebox.showerror(APP_NAME, "Some locations could not be changed:\n\n" + "\n".join(f"{row['sample']}: {error}" for row, error in failures[:5]))
        else: self.status.configure(text=f"Changed the location of {changed} item{'s' if changed != 1 else ''} to {location}.", fg=self.colors["muted"])
        self.edit_saved_next_cell()
    def _locations_change_failed(self, error):
        self.hide_progress(); self.status.configure(text="The selected locations could not be changed.", fg=self.colors["error"]); self.set_controls_busy(False); self.print_button.configure(state="normal" if self.rows else "disabled"); messagebox.showerror(APP_NAME, error)
    def choose_print_layout(self):
        """Show print-only column and page-break choices before opening macOS Print."""
        print_keys = tuple(key for key in self.keys if key != "structure") + PRINT_CHECKLIST_KEYS
        preset = print_preset(self.profile_settings.get("print_preset"), print_keys)
        dialog = tk.Toplevel(self)
        dialog.title("Print layout")
        dialog.transient(self)
        dialog.minsize(760, 430)
        body = ttk.Frame(dialog, padding=18)
        body.pack(fill="both", expand=True)
        print_order = list(preset["column_order"])
        visible = {key: tk.BooleanVar(value=key in preset["visible_columns"]) for key in print_keys}
        names = dict(zip(self.keys, self.headings))
        names.update(dict(zip(PRINT_CHECKLIST_KEYS, PRINT_CHECKLIST_HEADINGS)))
        ttk.Label(body, text="Preview (drag columns to reorder)").grid(row=0, column=0, columnspan=3, sticky="w")
        preview_frame = ttk.Frame(body)
        preview_frame.grid(row=1, column=0, columnspan=3, sticky="nsew", pady=(5, 0))
        preview = ttk.Treeview(preview_frame, columns=print_keys, show="headings", height=10)
        preview.tag_configure("alternate", background="#38383a" if self.dark_mode else "#eeeeee")
        preview.grid(row=0, column=0, sticky="nsew")
        preview_scroll = ttk.Scrollbar(preview_frame, orient="horizontal", command=preview.xview)
        preview_scroll.grid(row=1, column=0, sticky="ew")
        preview.configure(xscrollcommand=preview_scroll.set)
        preview_frame.columnconfigure(0, weight=1)
        for key in print_keys:
            preview.heading(key, text=names[key])
            preview.column(key, width=105, minwidth=65, anchor="center" if key in {"decoupled", "inventory_sample", *PRINT_CHECKLIST_KEYS} else "w")
        for index, row in enumerate(self.rows[:10]):
            preview.insert("", "end", iid=f"preview-{index}", values=tuple(displayed_row_value(row, key) if key not in PRINT_CHECKLIST_KEYS else "" for key in print_keys), tags=("alternate",) if index % 2 else ())
        def displayed():
            return tuple(key for key in print_order if visible[key].get())
        def refresh_preview():
            preview.configure(displaycolumns=displayed())
        column_button = ttk.Menubutton(body, text="Columns")
        column_menu = tk.Menu(column_button, tearoff=False)
        column_button.configure(menu=column_menu)
        column_button.grid(row=2, column=0, sticky="w", pady=(10, 0))
        def toggle(key):
            if not displayed():
                visible[key].set(True)
                return
            refresh_preview()
        for key in print_keys:
            column_menu.add_checkbutton(label=names[key], variable=visible[key], command=lambda key=key: toggle(key))
        drag = {"source": None, "target": None, "moving": False}
        feedback = {"view": None}
        def header_key(event):
            if preview.identify_region(event.x, event.y) != "heading":
                return None
            column = preview.identify_column(event.x)
            try:
                return displayed()[int(column[1:]) - 1]
            except (IndexError, ValueError):
                return None
        def begin_drag(event):
            drag.update(source=header_key(event), target=None, moving=False)
            return "break" if drag["source"] else None
        def move_drag(event):
            if not drag["source"]:
                return None
            target = header_key(event)
            if target:
                drag["target"] = target
                drag["moving"] = target != drag["source"]
                if feedback["view"] is None:
                    feedback["view"] = ColumnDragFeedback(preview, self.colors["accent"])
                feedback["view"].show(event)
            return "break"
        def finish_drag(event):
            source = drag["source"]
            target = header_key(event) or drag["target"]
            if source and drag["moving"] and target and source != target:
                print_order.remove(source)
                print_order.insert(print_order.index(target), source)
                refresh_preview()
            drag.update(source=None, target=None, moving=False)
            if feedback["view"] is not None:
                feedback["view"].hide()
            return "break" if source else None
        preview.bind("<ButtonPress-1>", begin_drag)
        preview.bind("<B1-Motion>", move_drag)
        preview.bind("<ButtonRelease-1>", finish_drag)
        refresh_preview()

        split_column = tk.StringVar(value=names.get(preset["split_column"], "Disabled"))
        split_threshold = tk.StringVar(value=str(preset["split_threshold"]))
        split = ttk.Frame(body, padding=10)
        split.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(16, 0))
        ttk.Label(split, text="Split pages by attribute:").grid(row=0, column=0, sticky="w")
        column_box = ttk.Combobox(split, textvariable=split_column, values=["Disabled"] + [names[key] for key in self.keys], state="readonly", width=19)
        column_box.grid(row=0, column=1, sticky="w", padx=(7, 0))
        ttk.Label(split, text="Minimum entries:").grid(row=0, column=2, sticky="w", padx=(18, 0))
        ttk.Entry(split, textvariable=split_threshold, width=8).grid(row=0, column=3, sticky="w", padx=(7, 0))

        result = {"value": None}
        def print_with_layout():
            selected = tuple(key for key in print_order if visible[key].get())
            if not selected:
                messagebox.showerror("Print layout", "Choose at least one column.", parent=dialog)
                return
            selected_name = split_column.get()
            selected_key = next((key for key, name in names.items() if name == selected_name), None)
            threshold = 10
            if selected_key:
                try:
                    threshold = int(split_threshold.get())
                    if threshold < 1:
                        raise ValueError
                except ValueError:
                    messagebox.showerror("Print layout", "Minimum entries must be a whole number of at least 1.", parent=dialog)
                    return
            result["value"] = {"column_order": print_order, "visible_columns": selected,
                               "split_column": selected_key,
                               "split_threshold": threshold}
            dialog.destroy()
        buttons = ttk.Frame(body)
        buttons.grid(row=6, column=0, columnspan=3, sticky="e", pady=(16, 0))
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right")
        print_action = ttk.Button(buttons, text="Print…", command=print_with_layout, default="active")
        print_action.pack(side="right", padx=(0, 8))
        dialog.bind("<Return>", lambda _event: print_with_layout())
        dialog.bind("<KP_Enter>", lambda _event: print_with_layout())
        print_action.focus_set()
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.grab_set()
        self.wait_window(dialog)
        return result["value"]
    def printable(self):
        """Create a location-separated A4 inventory verification checklist."""
        layout = self.choose_print_layout()
        if not layout:
            return
        self.profile_settings["print_preset"] = layout
        save_profile_settings(self.profile_settings)
        path = Path(tempfile.gettempdir()) / "Chemotion-inventory.pdf"
        try:
            rows = self.filtered_rows()
            write_inventory_pdf(path, rows, layout)
        except OSError as error:
            messagebox.showerror(APP_NAME, f"The PDF could not be created:\n\n{error}")
            return
        self.status.configure(text=f"Created a checklist with {len(rows)} items. Choose a printer in the print dialog.", fg=self.colors["muted"])
        # Ask PDFKit for a native NSPrintOperation. This presents the macOS
        # system print panel directly, without opening Preview or submitting a
        # job until the user confirms Print.
        script = f'''ObjC.import("AppKit");
ObjC.import("PDFKit");
const app = $.NSApplication.sharedApplication;
app.setActivationPolicy($.NSApplicationActivationPolicyAccessory);
const documentURL = $.NSURL.fileURLWithPath({json.dumps(str(path))});
const document = $.PDFDocument.alloc.initWithURL(documentURL);
const operation = document.printOperationForPrintInfoScalingModeAutoRotate(
    $.NSPrintInfo.sharedPrintInfo, 1, true
);
operation.setShowsPrintPanel(true);
operation.setShowsProgressPanel(true);
app.activateIgnoringOtherApps(true);
operation.runOperation;
'''
        if os.name == "nt":
            # Windows opens the generated PDF in the user's configured PDF
            # app, whose print command provides the system print dialog.
            os.startfile(str(path))
        else:
            subprocess.Popen(["osascript", "-l", "JavaScript", "-e", script])

