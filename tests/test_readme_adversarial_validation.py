"""
Adversarial Validation Test Harness for HostDrop README
=========================================================
Tests:
1. Markdown table parsing & structure (8-row 2-column feature table, 5-row 3-column speed table, scorecard table).
2. Sentence-case heading assertions across all Markdown headers (#, ##, ###).
3. Keyword & Semantic Assertions (Tagline, Inbox/Library distinction, 127.0.0.1 vs 192.168.x.x, /api/check, File System Access API).
4. Broken links, malformed markdown, HTML tag balance, code fences.
5. Zero em-dashes (\\u2014), zero en-dashes (\\u2013), zero AI buzzwords.
6. Workspace parity between peaceful-darwin and hostdrop.
"""

import os
import re
import sys
import urllib.parse

# Ensure UTF-8 output on Windows consoles
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

def run_adversarial_suite():
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    hostdrop_dir = os.path.expanduser("~/Documents/hostdrop")
    readme_path = os.path.join(base_dir, "README.md")
    readme_prev_path = os.path.join(base_dir, "README_previous.md")
    readme_latest_path = os.path.join(base_dir, "README_latest.md")
    comparison_path = os.path.join(base_dir, "readme_side_by_side_comparison.md")

    passed_count = 0
    failed_count = 0
    test_results = []

    def test_assert(name, condition, details=""):
        nonlocal passed_count, failed_count
        if condition:
            passed_count += 1
            test_results.append((name, True, details))
            print(f"  [PASS] {name}")
        else:
            failed_count += 1
            test_results.append((name, False, details))
            print(f"  [FAIL] {name}: {details}")

    print("\n=======================================================")
    print("  HOSTDROP README ADVERSARIAL VALIDATION SUITE")
    print("=======================================================\n")

    # ---------------------------------------------------------
    # SUITE 1: File Existence & Multi-Workspace Parity
    # ---------------------------------------------------------
    print("--- Suite 1: File Existence and Workspace Parity ---")
    test_assert("File exists: README.md", os.path.isfile(readme_path), f"Path not found: {readme_path}")
    for fname, path in [
        ("README_previous.md", readme_prev_path),
        ("README_latest.md", readme_latest_path),
        ("readme_side_by_side_comparison.md", comparison_path),
    ]:
        if os.path.isfile(path):
            test_assert(f"Optional reference file exists: {fname}", True)

    # Check hostdrop mirror directory if it exists
    if os.path.isdir(hostdrop_dir):
        target = os.path.join(hostdrop_dir, "README.md")
        exists = os.path.isfile(target)
        test_assert("File exists in hostdrop mirror: README.md", exists, f"Path not found: {target}")
        if exists:
            src_content = open(os.path.join(base_dir, "README.md"), "r", encoding="utf-8").read()
            dst_content = open(target, "r", encoding="utf-8").read()
            test_assert("Workspace parity: README.md", src_content == dst_content, "Content mismatch between workspaces")

    # Load README.md content
    with open(readme_path, "r", encoding="utf-8") as f:
        readme_text = f.read()
    readme_lines = readme_text.splitlines()

    # ---------------------------------------------------------
    # SUITE 2: Markdown Table Syntax & Exact Row/Column Structure
    # ---------------------------------------------------------
    print("\n--- Suite 2: Markdown Table Structure and Formatting ---")

    # Locate "What it does" Feature Table
    table_lines = []
    in_feature_table = False
    for line in readme_lines:
        if line.strip().startswith("| Feature | Details |"):
            in_feature_table = True
            table_lines.append(line.strip())
            continue
        if in_feature_table:
            if line.strip().startswith("|") and line.strip().endswith("|"):
                table_lines.append(line.strip())
            else:
                break

    test_assert("Feature table header found", len(table_lines) > 0, "Could not find table starting with '| Feature | Details |'")
    
    if len(table_lines) > 0:
        # Header + Separator + data rows
        test_assert("Feature table line count has header, sep, and data rows", len(table_lines) >= 10, f"Got {len(table_lines)} lines")
        
        # Check header
        header_cols = [c.strip() for c in table_lines[0].split("|")[1:-1]]
        test_assert("Feature table header columns == ['Feature', 'Details']", header_cols == ["Feature", "Details"], f"Got {header_cols}")

        # Check separator
        sep_cols = [c.strip() for c in table_lines[1].split("|")[1:-1]]
        test_assert("Feature table separator has 2 columns", len(sep_cols) == 2 and all(re.match(r"^:?-+:?$", c) for c in sep_cols), f"Got {sep_cols}")

        # Check all data rows
        data_rows = table_lines[2:]
        test_assert("Feature table has at least 8 data rows", len(data_rows) >= 8, f"Got {len(data_rows)} data rows")

        expected_features = [
            ("🔄 **Two-way sharing**", "Two-way sharing feature anchor"),
            ("📁 **Host drive navigator**", "Host drive navigator feature anchor"),
            ("⚡ **Full LAN speed**", "Full LAN speed feature anchor"),
            ("🔁 **Smart resume**", "Smart resume feature anchor"),
            ("📱 **Cross-platform**", "Cross-platform feature anchor"),
            ("🔌 **Direct Ethernet**", "Direct Ethernet feature anchor"),
            ("📡 **QR connect**", "QR connect feature anchor"),
            ("🗂️ **Dual folder download**", "Dual folder download feature anchor")
        ]

        for i, (expected_feature, desc) in enumerate(expected_features):
            if i < len(data_rows):
                row = data_rows[i]
                cols = [c.strip() for c in row.split("|")[1:-1]]
                test_assert(f"Feature table row {i+1} has exactly 2 columns", len(cols) == 2, f"Row: {row}")
                test_assert(f"Feature table row {i+1} starts with '{expected_feature}'", cols[0] == expected_feature, f"Got col0='{cols[0]}' vs expected '{expected_feature}'")
                test_assert(f"Feature table row {i+1} details non-empty", len(cols[1]) > 10, f"Details too short: '{cols[1]}'")

    # Locate "Connection types and speeds" Table
    speed_table_lines = []
    in_speed_table = False
    for line in readme_lines:
        if line.strip().startswith("| Connection type | Address format | Typical transfer speed |"):
            in_speed_table = True
            speed_table_lines.append(line.strip())
            continue
        if in_speed_table:
            if line.strip().startswith("|") and line.strip().endswith("|"):
                speed_table_lines.append(line.strip())
            else:
                break

    test_assert("Speed table header found", len(speed_table_lines) > 0, "Could not find '| Connection type | Address format | Typical transfer speed |'")
    if len(speed_table_lines) > 0:
        test_assert("Speed table line count is exactly 7 (header + sep + 5 data rows)", len(speed_table_lines) == 7, f"Got {len(speed_table_lines)} lines")
        data_speed_rows = speed_table_lines[2:]
        test_assert("Speed table has 5 data rows", len(data_speed_rows) == 5, f"Got {len(data_speed_rows)} rows")
        for i, row in enumerate(data_speed_rows):
            cols = [c.strip() for c in row.split("|")[1:-1]]
            test_assert(f"Speed table row {i+1} has exactly 3 columns", len(cols) == 3, f"Row: {row}")
            # Ensure speed uses 'to' notation and not en-dashes
            speed_col = cols[2]
            test_assert(f"Speed table row {i+1} uses 'to' notation in speed ({speed_col})", " to " in speed_col and "MB/s" in speed_col, f"Invalid speed notation: {speed_col}")

    # ---------------------------------------------------------
    # SUITE 3: Sentence-Case Heading Assertions
    # ---------------------------------------------------------
    print("\n--- Suite 3: Sentence-Case Heading Assertions ---")

    ALLOWED_PROPER_TERMS = {
        "HostDrop", "Windows", "Linux", "macOS", "iOS", "Android",
        "Python", "IP", "LAN", "Wi-Fi", "APIPA", "QR", "PC", "ZIP", "USB",
        "MIT", "PS5", "AP", "GB", "MB/s", "GHz", "Ethernet", "Chromium",
        "Safari", "Edge", "Firefox", "Opera", "Defender", "Firewall", "PowerShell",
        "Q&A", "Piyush", "Das", "GitHub", "API", "README", "UX", "UI",
        "Termux", "Cloudflare", "Pinggy", "SSH"
    }

    heading_regex = re.compile(r"^(#{1,6})\s+(.+)$")
    headings = []
    for line_num, line in enumerate(readme_lines, start=1):
        m = heading_regex.match(line.strip())
        if m:
            headings.append((line_num, m.group(1), m.group(2).strip()))

    test_assert("Headings found in README.md", len(headings) >= 9, f"Only found {len(headings)} headings")

    def check_sentence_case(heading_text):
        clean_text = re.sub(r"[`*_]", "", heading_text)
        words = clean_text.split()
        if not words:
            return True, "Empty heading"
        
        # Check first word capitalized
        first_word = re.sub(r"^[(\[\"']+|[)\]\"':,.]+$", "", words[0])
        if not (first_word[0].isupper() or first_word in ALLOWED_PROPER_TERMS):
            return False, f"First word '{first_word}' is not capitalized"

        # Check subsequent words
        for w in words[1:]:
            stripped_w = re.sub(r"^[(\[\"']+|[)\]\"':,.]+$", "", w)
            if not stripped_w:
                continue
            if stripped_w in ALLOWED_PROPER_TERMS:
                continue
            if stripped_w.startswith("README") or stripped_w.endswith(".md"):
                continue
            if re.match(r"^\d+(\.\d+)?(MB/s|GHz|MB|GB)?$", stripped_w):
                continue
            if stripped_w.islower():
                continue
            return False, f"Word '{stripped_w}' in heading '{heading_text}' is capitalized but not an allowed proper noun/acronym"
        
        return True, "OK"

    for line_num, level, htext in headings:
        is_sc, reason = check_sentence_case(htext)
        test_assert(f"Sentence-case: Line {line_num} ({level} {htext})", is_sc, reason)

    # ---------------------------------------------------------
    # SUITE 4: Core Keyword & Architectural Mental Model Assertions
    # ---------------------------------------------------------
    print("\n--- Suite 4: Keyword and Mental Model Assertions ---")

    # 1. Punchy Tagline
    test_assert("Tagline exact match: 'No cloud. No accounts. No USB. Just open a browser.'",
                "No cloud. No accounts. No USB. Just open a browser." in readme_text,
                "Tagline missing or altered")

    # 2. Secondary tagline / speed hook
    test_assert("Secondary tagline present",
                "Transfer files between any two devices on the same local network at full LAN speed." in readme_text,
                "Secondary tagline missing")

    # 3. Two-way sharing mental model (Inbox vs Library)
    test_assert("Inbox (Sent to PC) terminology present",
                "Inbox (Sent to PC)" in readme_text,
                "Inbox (Sent to PC) missing")
    test_assert("Library (Shared by PC) terminology present",
                "Library (Shared by PC)" in readme_text,
                "Library (Shared by PC) missing")

    # 4. Absence of legacy / confusing terminology
    test_assert("Legacy 'HOST SHARE PANEL' absent", "HOST SHARE PANEL" not in readme_text, "Found legacy term 'HOST SHARE PANEL'")
    test_assert("Legacy 'RECEIVE PANEL' absent", "RECEIVE PANEL" not in readme_text, "Found legacy term 'RECEIVE PANEL'")

    # 5. Host IP vs Network IP
    test_assert("Host loopback 127.0.0.1:8080 documented as Host Only / no QR",
                "127.0.0.1:8080" in readme_text and "Host Only" in readme_text and ("no qr" in readme_text.lower() or "does not generate a qr" in readme_text.lower()),
                "Host loopback / no QR distinction missing")
    test_assert("Network IP 192.168.x.x documented with QR code",
                "192.168.x.x" in readme_text and "QR" in readme_text,
                "Network IP / QR code connection missing")
    test_assert("APIPA 169.254.x.x direct ethernet documented",
                "169.254.x.x" in readme_text and "Ethernet" in readme_text,
                "APIPA ethernet connection missing")

    # 6. Smart resume and /api/check
    test_assert("Endpoint /api/check documented for smart resume",
                "/api/check" in readme_text,
                "Missing /api/check reference")
    test_assert("Byte offset resume explanation present",
                "resume" in readme_text.lower() and "byte" in readme_text.lower(),
                "Smart resume byte explanation missing")

    # 7. Folder download: Streaming ZIP vs File System Access API
    test_assert("Download ZIP option documented",
                "Download ZIP" in readme_text,
                "Download ZIP option missing")
    test_assert("File System Access API documented for direct folder writing",
                "File System Access API" in readme_text,
                "File System Access API missing")

    # 8. Drive Navigator
    test_assert("Host drive navigator documented (capacity bars, breadcrumbs, search)",
                "Host drive navigator" in readme_text or "drive capacity bars" in readme_text,
                "Drive navigator capabilities missing")

    # ---------------------------------------------------------
    # SUITE 5: Humanizer & Style Constraints (Zero Em/En Dashes, Zero Buzzwords)
    # ---------------------------------------------------------
    print("\n--- Suite 5: Humanizer and Style Constraints ---")

    # Zero em-dashes (\u2014)
    em_dash_count = readme_text.count("\u2014")
    test_assert("Zero em-dashes (U+2014) in README.md", em_dash_count == 0, f"Found {em_dash_count} em-dashes")

    # Zero en-dashes (\u2013)
    en_dash_count = readme_text.count("\u2013")
    test_assert("Zero en-dashes (U+2013) in README.md", en_dash_count == 0, f"Found {en_dash_count} en-dashes")

    # Zero AI buzzwords
    FORBIDDEN_BUZZWORDS = [
        r"\bseamless\b", r"\bseamlessly\b", r"\brevolutionize\b", r"\bcutting-edge\b",
        r"\bgame-changer\b", r"\bdelve\b", r"\btapestry\b", r"\bsupercharge\b",
        r"\brobust\b", r"\bleverage\b", r"\butilize\b", r"\beffortless\b",
        r"\bboasts\b", r"\bbeacon\b", r"\btestament\b", r"\bunleash\b",
        r"\bempower\b", r"\bparadigm\b", r"\bsynergy\b", r"\bpivotal\b",
        r"\bgroundbreaking\b", r"\bstate-of-the-art\b", r"\bnext-generation\b", r"\bholistic\b"
    ]

    buzzword_hits = []
    for pattern in FORBIDDEN_BUZZWORDS:
        matches = re.findall(pattern, readme_text, flags=re.IGNORECASE)
        if matches:
            buzzword_hits.extend(matches)

    test_assert("Zero AI buzzwords in README.md", len(buzzword_hits) == 0, f"Found forbidden buzzwords: {buzzword_hits}")

    # ---------------------------------------------------------
    # SUITE 6: Broken Links, HTML Tag Balance & Code Fences
    # ---------------------------------------------------------
    print("\n--- Suite 6: Markdown Syntax, HTML Tags, Links and Code Fences ---")

    # HTML Tag balance
    open_p = len(re.findall(r"<p\b[^>]*>", readme_text))
    close_p = len(re.findall(r"</p>", readme_text))
    test_assert("HTML <p> tag balance in README.md", open_p == close_p, f"<p> count: {open_p}, </p> count: {close_p}")

    open_strong = len(re.findall(r"<strong>", readme_text))
    close_strong = len(re.findall(r"</strong>", readme_text))
    test_assert("HTML <strong> tag balance in README.md", open_strong == close_strong, f"<strong>: {open_strong}, </strong>: {close_strong}")

    open_a = len(re.findall(r"<a\b[^>]*>", readme_text))
    close_a = len(re.findall(r"</a>", readme_text))
    test_assert("HTML <a> tag balance in README.md", open_a == close_a, f"<a>: {open_a}, </a>: {close_a}")

    # Code fences balance
    code_fences = len(re.findall(r"^\s*```", readme_text, flags=re.MULTILINE))
    test_assert("Markdown code fences (```) are even/balanced", code_fences % 2 == 0 and code_fences >= 4, f"Found {code_fences} fences")

    # Image src URLs well-formed
    img_tags = re.findall(r'<img\s+src="([^"]+)"', readme_text)
    test_assert("Found header badges (<img src=...>)", len(img_tags) >= 5, f"Found {len(img_tags)} img tags")
    for src in img_tags:
        is_valid_url = src.startswith("https://") or src.startswith("http://")
        test_assert(f"Badge URL valid format: {src[:45]}...", is_valid_url, f"Invalid URL: {src}")

    # Markdown links syntax [text](url)
    md_links = re.findall(r'\[([^\]]+)\]\(([^)]+)\)', readme_text)
    for ltext, lurl in md_links:
        test_assert(f"Markdown link format valid: [{ltext}]({lurl[:30]}...)", bool(re.match(r"^(https?://|#|mailto:|[a-zA-Z0-9_\-\.]+\.md).*", lurl)), f"Suspicious URL: {lurl}")

    # ---------------------------------------------------------
    # SUITE 7: Side-by-Side Comparison Document Audit
    # ---------------------------------------------------------
    print("\n--- Suite 7: Side-by-Side Comparison Document Audit ---")
    if not os.path.isfile(comparison_path):
        print("  [INFO] Comparison document not present in production repo. Skipping Suite 7.")
    else:
        with open(comparison_path, "r", encoding="utf-8") as f:
            comp_text = f.read()
        comp_lines = comp_text.splitlines()

        # Verify all 5 dimensions covered
        for dim_num, dim_title in [
            (1, "Header and tagline impact"),
            (2, "Feature scanning speed"),
            (3, "Architectural mental models"),
            (4, "Host vs network IP distinction"),
            (5, "Style and humanizer compliance")
        ]:
            found = any(f"Dimension {dim_num}" in l or dim_title.lower() in l.lower() for l in comp_lines)
            test_assert(f"Comparison covers Dimension {dim_num} ({dim_title})", found, f"Dimension {dim_num} not identified in {comparison_path}")

        # Check sentence-case on comparison doc headings
        comp_headings = []
        for lnum, line in enumerate(comp_lines, start=1):
            m = heading_regex.match(line.strip())
            if m:
                comp_headings.append((lnum, m.group(1), m.group(2).strip()))
        
        comp_sc_fails = []
        for lnum, level, htext in comp_headings:
            is_sc, reason = check_sentence_case(htext)
            if not is_sc:
                comp_sc_fails.append(f"Line {lnum} ({level} {htext}): {reason}")
        test_assert("Sentence-case in readme_side_by_side_comparison.md", len(comp_sc_fails) == 0, f"Violations: {comp_sc_fails}")

        # Zero em-dashes in comparison document
        comp_em = comp_text.count("\u2014")
        comp_en = comp_text.count("\u2013")
        test_assert("Zero em-dashes in comparison doc", comp_em == 0, f"Found {comp_em} em-dashes")
        test_assert("Zero en-dashes in comparison doc", comp_en == 0, f"Found {comp_en} en-dashes")

    # ---------------------------------------------------------
    # SUMMARY
    # ---------------------------------------------------------
    print("\n=======================================================")
    print(f"  TOTAL ASSERTIONS: {passed_count + failed_count}")
    print(f"  PASSED: {passed_count}")
    print(f"  FAILED: {failed_count}")
    print(f"  FINAL VERDICT: {'APPROVE (100% PASS)' if failed_count == 0 else 'REJECT'}")
    print("=======================================================\n")

    return failed_count == 0

if __name__ == "__main__":
    success = run_adversarial_suite()
    sys.exit(0 if success else 1)
