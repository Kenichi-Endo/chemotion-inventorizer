"""Inventory PDF layout and generation."""
from .settings import (
    print_preset,
)
from .inventory import (
    INVENTORY_HEADINGS,
    INVENTORY_KEYS,
    PRINT_CHECKLIST_HEADINGS,
    PRINT_CHECKLIST_KEYS,
    displayed_row_value,
    natural_sort_value,
    shown,
)


def pdf_text(value):
    """Encode text for a standard PDF font without letting PDF syntax leak in."""
    value = str(value).encode("cp1252", "replace").decode("cp1252")
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def location_group(location):
    normalized = " ".join(str(location).casefold().split())
    if normalized.startswith("0.563"):
        return "0.563"
    if normalized.startswith("0.830") and "cabinet 138" in normalized:
        return "0.830 cabinet 138"
    if normalized.startswith("0.830") and "cabinet 635" in normalized:
        return "0.830 cabinet 635"
    if normalized.startswith("0.830"):
        return "0.830 others"
    return "Other locations"


def write_inventory_pdf(path, rows, layout=None):
    """Write a compact, dependency-free, portrait A4 PDF checklist."""
    page_width, page_height = 595.28, 841.89  # A4 portrait, in PostScript points.
    margin, line_height = 28, 12
    all_columns = tuple(key for key in INVENTORY_KEYS if key != "structure") + PRINT_CHECKLIST_KEYS
    layout = print_preset(layout, all_columns)
    printed_keys = tuple(key for key in layout["column_order"] if key in layout["visible_columns"])
    columns = inventory_pdf_columns(rows, page_width - 2 * margin, printed_keys)

    def sort_label(row):
        return natural_sort_value(row["label"])

    pages = []
    split_key = layout["split_column"]
    if split_key:
        groups = {}
        for row in rows:
            group = shown(row.get(split_key))
            groups.setdefault(group, []).append(row)
        unsplit_rows = []
        for group in sorted(groups, key=str.casefold):
            group_rows = sorted(groups[group], key=sort_label)
            threshold = layout["split_threshold"]
            if len(group_rows) < threshold:
                unsplit_rows.extend(group_rows)
                continue
            if unsplit_rows:
                pages.extend(pdf_table_pages("", unsplit_rows, columns, page_width, page_height, margin, line_height, printed_keys))
                unsplit_rows = []
            pages.extend(pdf_table_pages(f" — {group}", group_rows, columns, page_width, page_height, margin, line_height, printed_keys))
        if unsplit_rows:
            pages.extend(pdf_table_pages("", unsplit_rows, columns, page_width, page_height, margin, line_height, printed_keys))
    elif rows:
        pages.extend(pdf_table_pages("", sorted(rows, key=lambda row: (str(row["location"]).casefold(), sort_label(row))), columns, page_width, page_height, margin, line_height, printed_keys))
    if not pages:
        pages = [pdf_table_pages("", [], columns, page_width, page_height, margin, line_height, printed_keys)[0]]

    catalog = "<< /Type /Catalog /Pages 2 0 R >>"
    page_objects, content_objects, page_ids = [], [], []
    for index, stream in enumerate(pages):
        content_id, page_id = 5 + index * 2, 6 + index * 2
        content_objects.append((content_id, f"<< /Length {len(stream.encode('cp1252', 'replace'))} >>\nstream\n{stream}\nendstream"))
        page_objects.append((page_id, f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_width:.2f} {page_height:.2f}] /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {content_id} 0 R >>"))
        page_ids.append(page_id)
    # Text is encoded as Windows-1252 (WinAnsi in PDF terminology).  Declare
    # that explicitly: the base PDF font's default StandardEncoding maps some
    # extended characters, including ®, to different glyphs.
    pdf_objects = [catalog, f"<< /Type /Pages /Kids [{' '.join(f'{ref} 0 R' for ref in page_ids)}] /Count {len(page_ids)} >>",
                   "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
                   "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"]
    for (_content_id, content), (_page_id, page) in zip(content_objects, page_objects): pdf_objects.extend((content, page))
    output = "%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"; offsets = [0]
    for number, obj in enumerate(pdf_objects, 1):
        offsets.append(len(output.encode("cp1252"))); output += f"{number} 0 obj\n{obj}\nendobj\n"
    startxref = len(output.encode("cp1252")); output += f"xref\n0 {len(pdf_objects) + 1}\n0000000000 65535 f \n" + "".join(f"{offset:010d} 00000 n \n" for offset in offsets[1:]) + f"trailer\n<< /Size {len(pdf_objects) + 1} /Root 1 0 R >>\nstartxref\n{startxref}\n%%EOF\n"
    path.write_bytes(output.encode("cp1252", "replace"))


def inventory_pdf_columns(rows, available_width, printed_keys=("location", "label", "molecule", "amount")):
    """Size the variable PDF columns from the values they actually contain."""
    headings = dict(zip(INVENTORY_KEYS, INVENTORY_HEADINGS))
    headings.update(dict(zip(PRINT_CHECKLIST_KEYS, PRINT_CHECKLIST_HEADINGS)))
    fields = tuple((headings[key], key, 42 if key in {"label", "amount", "decoupled", "inventory_sample"} else 65,
                    190 if key in {"molecule", "sample", "location"} else 125)
                   for key in printed_keys)
    # Helvetica at 9pt averages roughly 4.5pt per character.  The caps retain
    # useful wrapping for unusually long names instead of letting one value
    # consume the entire page.
    desired = []
    for heading, key, minimum, maximum in fields:
        longest = max((len(displayed_row_value(row, key)) for row in rows), default=0)
        desired.append(min(maximum, max(minimum, (max(longest, len(heading)) * 4.5) + 6)))
    variable_width = available_width
    total = sum(desired)
    if total > variable_width:
        minimums = [field[2] for field in fields]
        shrinkable = total - sum(minimums)
        reduction = total - variable_width
        if shrinkable:
            desired = [minimum + (width - minimum) * max(0, 1 - reduction / shrinkable)
                       for width, minimum in zip(desired, minimums)]
        else:
            desired = [variable_width * width / total for width in desired]
    elif total < variable_width:
        # Use spare width for the text-heavy fields rather than leaving a blank
        # strip beside the table, without exceeding a sensible readable cap.
        spare = variable_width - total
        weights = tuple(3 if key in {"molecule", "sample", "location"} else 1
                        for _heading, key, _minimum, _maximum in fields)
        while spare > 0.01:
            eligible = [index for index, field in enumerate(fields) if desired[index] < field[3] - 0.01]
            if not eligible: break
            weight_total = sum(weights[index] for index in eligible)
            used = 0
            for index in eligible:
                maximum = fields[index][3]
                added = min(maximum - desired[index], spare * weights[index] / weight_total)
                desired[index] += added; used += added
            spare -= used
    sized = tuple((heading, width) for (heading, _key, _minimum, _maximum), width in zip(fields, desired))
    return sized


def pdf_table_pages(group, rows, columns, page_width, page_height, margin, line_height, printed_keys):
    x_positions, current = [], margin
    for _title, width in columns: x_positions.append(current); current += width
    table_width = current - margin
    pages, page, y = [], [], page_height - margin
    def text(x, y, value, font="F1", size=9): page.append(f"BT /{font} {size} Tf {x:.1f} {y:.1f} Td ({pdf_text(value)}) Tj ET")
    def new_page(continued=False):
        nonlocal page, y
        if page: pages.append("\n".join(page))
        page, y = [], page_height - margin
        text(margin, y, f"CHEMICAL INVENTORY{group}" + (" (continued)" if continued else ""), "F2", 14); y -= 19
        y -= 15
        page.append(f"0.45 G 0.6 w {margin:.1f} {y - 14:.1f} {table_width:.1f} 16 re S")
        for x, (title, _width) in zip(x_positions, columns): text(x + 2, y - 10, title, "F2", 8)
        y -= 16
    new_page()
    for row_index, row in enumerate(rows):
        values = tuple("" if key in PRINT_CHECKLIST_KEYS else
                       ("Used up" if key == "amount" and "used up" in row["sample"].casefold() else displayed_row_value(row, key))
                       for key in printed_keys)
        height = line_height + 5
        if y - height < margin: new_page(True)
        if row_index % 2:
            page.append(f"0.94 g {margin:.1f} {y - height:.1f} {table_width:.1f} {height:.1f} re f 0 g")
        page.append(f"0.45 G 0.5 w {margin:.1f} {y - height:.1f} {table_width:.1f} {height:.1f} re S")
        for x in x_positions[1:]: page.append(f"0.45 G {x:.1f} {y - height:.1f} m {x:.1f} {y:.1f} l S")
        for index, (x, value, (_heading, width)) in enumerate(zip(x_positions, values, columns)):
            page.append(f"q {x:.1f} {y - height:.1f} {width:.1f} {height:.1f} re W n")
            text(x + 2, y - 10, value, "F2" if printed_keys[index:index + 1] == ("label",) else "F1")
            page.append("Q")
        y -= height
    pages.append("\n".join(page))
    return pages

