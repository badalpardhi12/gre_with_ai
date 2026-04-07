"""Parse xlsx GRE question files without openpyxl."""
import zipfile
import xml.etree.ElementTree as ET


def parse_xlsx(path):
    """Parse xlsx file, return list of rows (each row is list of strings)."""
    with zipfile.ZipFile(path, 'r') as zf:
        # Parse shared strings
        shared = []
        with zf.open('xl/sharedStrings.xml') as f:
            tree = ET.parse(f)
            ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            for si in tree.findall('.//s:si', ns):
                texts = si.findall('.//s:t', ns)
                shared.append(''.join(t.text or '' for t in texts))

        # Parse sheet1
        with zf.open('xl/worksheets/sheet1.xml') as f:
            tree = ET.parse(f)
            ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            rows = []
            for row in tree.findall('.//s:row', ns):
                cells = []
                for cell in row.findall('s:c', ns):
                    t = cell.get('t', '')
                    val_el = cell.find('s:v', ns)
                    if val_el is not None and val_el.text:
                        if t == 's':
                            cells.append(shared[int(val_el.text)])
                        else:
                            cells.append(val_el.text)
                    else:
                        cells.append('')
                rows.append(cells)
            return rows


if __name__ == '__main__':
    for fname in ['data/external/gre_questions1.csv', 'data/external/gre_questions2.csv']:
        rows = parse_xlsx(fname)
        print(f"\n{fname}: {len(rows)} rows")
        if rows:
            print(f"First row cols: {len(rows[0])}")
            for i, row in enumerate(rows[:3]):
                print(f"  Row {i}: {[c[:60]+'...' if len(c) > 60 else c for c in row]}")
            if len(rows) > 3:
                print(f"  ... {len(rows)-3} more rows")
