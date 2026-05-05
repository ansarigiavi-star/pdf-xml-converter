"""
PDF → IATA IS-XML Converter — Web App (Railway)
Converts ground handler invoices (PDF) to IATA IS-XML format
(IS-XML Invoice Standard V3.4, Charge Category: Ground Handling)
"""

import io
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from xml.dom import minidom

import pdfplumber
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024


# ── PDF field extraction ─────────────────────────────────────────────────────

def extract_invoice_fields(pdf_bytes: bytes) -> dict:
    """
    Extract structured fields from a ground handler invoice PDF.
    Returns a dict of all detected fields.
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        full_text = "\n".join(
            page.extract_text(x_tolerance=3, y_tolerance=3) or ""
            for page in pdf.pages
        )

    lines = [l.strip() for l in full_text.splitlines() if l.strip()]
    text = "\n".join(lines)

    def find(patterns, default=""):
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                return m.group(1).strip()
        return default

    # ── Seller (ground handler / supplier) ──────────────────────────────────
    seller_name    = find([r'^([A-Z][^\n]{2,40}(?:Ltd|LLC|Inc|GmbH|SA|SAS|BV|NV|Handling|Services)[^\n]*)', r'(?:from|supplier)[:\s]+([^\n]+)'])
    seller_address = find([r'((?:Unit|Floor|Suite|No\.?)\s*\d[^\n]*)'])
    seller_email   = find([r'[Ee]mail[:\s]+([^\s]+@[^\s]+)'])
    seller_tel     = find([r'[Tt]el[:\s]+([\d\s\+\-\(\)]{7,20})'])
    seller_vat     = find([r'VAT\s+(?:Registration\s+)?(?:Number|No\.?)[:\s]+([\w\s]+?)(?:\s+Company|\s*$)', r'VAT[:\s#]+([\w\s]+?)(?:\s+Company|\s*$)'])
    seller_reg     = find([r'Company\s+Registration\s+(?:Number|No\.?)[:\s]+([\w\s]+?)(?:\s*$)', r'Reg(?:istration)?[:\s#No\.]+([\w]+)'])
    seller_iban    = find([r'IBAN[:\s]+([\w]+)'])
    seller_swift   = find([r'SWIFT\s*(?:CODE\s*(?:\([^)]+\))?\s*IS\s+|[:\s]+)([\w]+)'])
    seller_bank    = find([r'((?:Natwest|Barclays|HSBC|Lloyds|BNP|Deutsche|UniCredit)[^\n]*)'])
    seller_acc_no  = find([r'Acc(?:ount)?\s+Number[:\s]+([\d]+)'])
    seller_sort    = find([r'Sort\s+Code[:\s]+([\d\-]+)'])

    # ── Buyer (airline) ─────────────────────────────────────────────────────
    buyer_name     = find([r'(?:Client|Bill(?:ed)?\s+To|To)[:\s]*\n([^\n]+)', r'(Aegean Airlines[^\n]*)'])
    buyer_iata     = find([r'\b(A3|[A-Z]{2})\s*[-–]\s*([A-Z][a-z]+)', r'([A-Z]{2})\s*-\s*Aegean'])
    buyer_location = find([r'(?:Room|Office|Terminal)[^\n]+\n([^\n]+Airport[^\n]*)'])

    # ── Invoice header ───────────────────────────────────────────────────────
    inv_number  = find([r'Invoice\s+No[:\s.]*(\d+)', r'Invoice\s+#[:\s]*(\d+)', r'Inv(?:oice)?[#No\.\s]+(\d+)'])
    inv_date    = find([r'Invoice\s+Date[:\s]+([\d]{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4})', r'Date[:\s]+([\d]{1,2}[/\-]\d{1,2}[/\-]\d{4})'])
    pay_terms   = find([r'Payment\s+Terms[:\s]+(\d+)\s+days', r'(\d+)\s+days?\s+from'])
    currency    = find([r'\b(GBP|EUR|USD|CHF|AED|SAR)\b'], 'GBP')

    # ── Line items / station costs ───────────────────────────────────────────
    # Extract station + cost pairs  e.g. "LHR - HEATHROW  911.26"
    station_lines = re.findall(r'([A-Z]{3})\s*[-–]\s*([A-Z\s]+?)\s+([\d,]+\.?\d*)\s*$', text, re.MULTILINE)

    # ── Totals ───────────────────────────────────────────────────────────────
    total_ex_vat = find([r'Total\s+Ex\s+Vat[^\d]*([\d,]+\.?\d*)', r'Subtotal[^\d]*([\d,]+\.?\d*)'])
    total_vat    = find([r'Total\s+VAT[^\d]*([\d,]+\.?\d*)', r'VAT\s+Amount[^\d]*([\d,]+\.?\d*)'])
    total_gross  = find([r'Total\s+Invoice\s+Cost[^\d]*([\d,]+\.?\d*)', r'Total\s+(?:Amount\s+)?Due[^\d]*([\d,]+\.?\d*)', r'TOTAL[^\d]*([\d,]+\.?\d*)'])
    vat_rate     = find([r'@\s*([\d.]+)\s*%', r'VAT\s+@\s*([\d.]+)\s*%'], '0')

    # ── Service / charge code detection ─────────────────────────────────────
    charge_code = "Misc"
    charge_map = {
        "Mishandling Baggage":  ["mishandl", "damaged bag", "bag repair", "bag replac"],
        "Baggage":              ["baggage handl", "bag handl", "loading", "unloading"],
        "Baggage Delivery":     ["bag deliver", "baggage deliver"],
        "Ramp Handling":        ["ramp handl", "ramp service"],
        "Passenger Handling":   ["passenger handl", "pax handl"],
        "Catering":             ["catering", "meal", "inflight"],
        "Cleaning":             ["cleaning", "cabin clean"],
        "Deicing":              ["de-ic", "deic", "anti-ic"],
        "Cargo Handling":       ["cargo handl"],
        "Crew Accommodation":   ["crew hotel", "crew accommod"],
        "Crew Transportation":  ["crew transport", "crew shuttle"],
    }
    tl = text.lower()
    for code, keywords in charge_map.items():
        if any(kw in tl for kw in keywords):
            charge_code = code
            break

    # Parse invoice date into ISO format
    inv_date_iso = ""
    if inv_date:
        for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y"):
            try:
                inv_date_iso = datetime.strptime(inv_date, fmt).strftime("%Y-%m-%d")
                break
            except ValueError:
                continue
    if not inv_date_iso:
        inv_date_iso = datetime.today().strftime("%Y-%m-%d")

    return {
        "seller_name":    seller_name    or "Unknown Supplier",
        "seller_address": seller_address or "",
        "seller_email":   seller_email   or "",
        "seller_tel":     seller_tel     or "",
        "seller_vat":     seller_vat     or "",
        "seller_reg":     seller_reg     or "",
        "seller_iban":    seller_iban    or "",
        "seller_swift":   seller_swift   or "",
        "seller_bank":    seller_bank    or "",
        "seller_acc_no":  seller_acc_no  or "",
        "seller_sort":    seller_sort    or "",
        "buyer_name":     buyer_name     or "Unknown Airline",
        "buyer_iata":     buyer_iata     or "",
        "buyer_location": buyer_location or "",
        "inv_number":     inv_number     or "UNKNOWN",
        "inv_date":       inv_date_iso,
        "pay_terms":      pay_terms      or "30",
        "currency":       currency,
        "charge_code":    charge_code,
        "station_lines":  station_lines,  # list of (iata, name, amount)
        "total_ex_vat":   total_ex_vat   or "0.00",
        "total_vat":      total_vat      or "0.00",
        "total_gross":    total_gross    or "0.00",
        "vat_rate":       vat_rate,
        "raw_text":       text,
    }


# ── IATA IS-XML builder ──────────────────────────────────────────────────────

def build_iata_xml(fields: dict, filename: str) -> str:
    """
    Build IATA IS-XML Invoice Standard V3.4 — full structure.

    Hierarchy:
      Transmission
        TransmissionHeader
        Invoice (1..n)
          InvoiceHeader
            SellerOrganization  (OrganizationID, OrganizationDesignator,
                                 OrganizationName1, TaxRegistrationID,
                                 CompanyRegistrationID, Address{…},
                                 ContactDetails{…}, BankDetails{…})
            BuyerOrganization   (same sub-elements)
            PaymentTerms
            ISDetails
          LineItem (1..n)
            LineItemDetail (1..n)   ← new in V3.4 full spec
              ChargeCode-specific mandatory fields
              AddOnCharges (VAT as add-on per spec §2.3)
            AddOnCharges            ← line-item-level add-ons if any
          InvoiceSummary
            AddOnCharges            ← invoice-level add-ons
        TransmissionSummary
    """
    ns = "http://www.iata.org/IATA/2007/00"
    ET.register_namespace("", ns)

    def el(parent, tag, text=None, **attrs):
        e = ET.SubElement(parent, tag, **attrs)
        if text is not None:
            e.text = str(text)
        return e

    def money(value: str) -> str:
        """Normalise monetary string to 2dp float string."""
        try:
            return f"{float(value.replace(',', '')):.2f}"
        except (ValueError, AttributeError):
            return "0.00"

    now_iso   = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    inv_date  = fields["inv_date"]
    currency  = fields["currency"]
    vat_rate  = fields["vat_rate"]
    has_vat   = bool(vat_rate) and float(vat_rate) > 0

    station_lines = fields["station_lines"] or [("", "", fields["total_ex_vat"])]

    # ── Root ─────────────────────────────────────────────────────────────────
    transmission = ET.Element("Transmission", xmlns=ns)

    # ── TransmissionHeader ───────────────────────────────────────────────────
    th = el(transmission, "TransmissionHeader")
    el(th, "TransmissionDateTime", now_iso)          # V3.4: DateTime not Date
    el(th, "Version",              "3.4")
    el(th, "BillingCategory",      "Miscellaneous")

    # ── Invoice ──────────────────────────────────────────────────────────────
    invoice = el(transmission, "Invoice")

    # ── InvoiceHeader ────────────────────────────────────────────────────────
    ih = el(invoice, "InvoiceHeader")
    el(ih, "InvoiceNumber",  fields["inv_number"])
    el(ih, "InvoiceDate",    inv_date)
    el(ih, "InvoiceType",    "Original")
    el(ih, "ChargeCategory", "Ground Handling")

    # ── SellerOrganization ───────────────────────────────────────────────────
    seller = el(ih, "SellerOrganization")
    el(seller, "OrganizationID",         fields["seller_name"])
    el(seller, "OrganizationDesignator", "GH")          # Ground Handler designator
    el(seller, "OrganizationName1",      fields["seller_name"])
    if fields["seller_vat"]:
        el(seller, "TaxRegistrationID",     fields["seller_vat"].strip())
    if fields["seller_reg"]:
        el(seller, "CompanyRegistrationID", fields["seller_reg"].strip())

    # Structured Address block (V3.4 spec §2.1)
    addr = el(seller, "Address")
    if fields["seller_address"]:
        el(addr, "AddressLine1", fields["seller_address"])
    # Extract city/postcode from address if available
    addr_text = fields.get("seller_address", "")
    postcode_m = re.search(r'\b([A-Z]{1,2}\d{1,2}[A-Z]?\s*\d[A-Z]{2})\b', addr_text)
    if postcode_m:
        el(addr, "PostalCode", postcode_m.group(1))
    el(addr, "CountryCode",      "GB")   # default; override if detected
    el(addr, "CountryName",      "United Kingdom")

    # ContactDetails — Phone
    if fields["seller_tel"]:
        cd = el(seller, "ContactDetails")
        el(cd, "ContactType",  "Phone")
        el(cd, "ContactValue", fields["seller_tel"].strip())

    # ContactDetails — Email
    if fields["seller_email"]:
        cd = el(seller, "ContactDetails")
        el(cd, "ContactType",  "Email")
        el(cd, "ContactValue", fields["seller_email"])

    # BankDetails (TPA_Extension — widely used in practice)
    if fields["seller_iban"] or fields["seller_swift"] or fields["seller_acc_no"]:
        bank = el(seller, "BankDetails")
        if fields["seller_bank"]:
            el(bank, "BankName",      fields["seller_bank"])
        if fields["seller_acc_no"]:
            el(bank, "AccountNumber", fields["seller_acc_no"])
        if fields["seller_sort"]:
            el(bank, "SortCode",      fields["seller_sort"])
        if fields["seller_iban"]:
            el(bank, "IBAN",          fields["seller_iban"])
        if fields["seller_swift"]:
            el(bank, "SWIFTCode",     fields["seller_swift"])

    # ── BuyerOrganization ────────────────────────────────────────────────────
    buyer = el(ih, "BuyerOrganization")
    el(buyer, "OrganizationID",         fields["buyer_name"])
    if fields["buyer_iata"]:
        el(buyer, "OrganizationDesignator", fields["buyer_iata"])
        el(buyer, "OrganizationName1",      fields["buyer_name"])
    if fields["buyer_location"]:
        el(buyer, "LocationID", fields["buyer_location"])

    # ── PaymentTerms ─────────────────────────────────────────────────────────
    pt = el(ih, "PaymentTerms")
    el(pt, "CurrencyCode",     currency)
    el(pt, "SettlementMethod", "IS")
    el(pt, "PaymentDays",      fields["pay_terms"])

    # ── ISDetails ─────────────────────────────────────────────────────────────
    isd = el(ih, "ISDetails")
    el(isd, "DigitalSignatureFlag",      "false")
    el(isd, "AttachmentIndicatorOriginal", "true")   # V3.4: PDF invoice = original attachment

    # ── LineItems ─────────────────────────────────────────────────────────────
    total_net_all     = 0.0
    total_addon_all   = 0.0   # VAT add-on total across all line items

    for idx, (iata_code, station_name, amount) in enumerate(station_lines, start=1):
        amt_net = float(money(amount))
        total_net_all += amt_net

        li = el(invoice, "LineItem")
        el(li, "LineItemNumber", str(idx))
        el(li, "ChargeCode",     fields["charge_code"])
        desc = f"{fields['charge_code']} – {iata_code} {station_name}".strip(" –")
        el(li, "Description",    desc or fields["charge_code"])
        el(li, "StartDate",      inv_date)
        el(li, "EndDate",        inv_date)

        qty = el(li, "Quantity", "1")
        qty.set("UOMCode", "EA")

        unit = el(li, "UnitPrice", f"{amt_net:.2f}")
        unit.set("SF", currency)

        charge = el(li, "ChargeAmount", f"{amt_net:.2f}")
        charge.set("Name", "NetAmount")

        # ── LineItemDetail ── (V3.4 full spec — charge-code-specific fields)
        lid = el(li, "LineItemDetail")
        el(lid, "LineItemDetailNumber", "1")
        if iata_code:
            el(lid, "LocationCode", iata_code)          # mandatory for Mishandling Baggage
        el(lid, "FlightDateTime",   inv_date + "T00:00:00Z")  # mandatory (substitution permitted)
        el(lid, "ReferenceNumber",  fields["inv_number"])     # WorldTracer / ref
        # MishandlingType for Mishandling Baggage charge code
        if fields["charge_code"] == "Mishandling Baggage":
            el(lid, "MishandlingType", "Damage")
        el(lid, "ChargeAmount", f"{amt_net:.2f}")
        el(lid, "TotalNetAmount", f"{amt_net:.2f}")

        # ── AddOnCharges at LineItemDetail level (VAT per §2.3) ──────────────
        if has_vat:
            vat_amt = round(amt_net * float(vat_rate) / 100, 2)
            total_addon_all += vat_amt
            aoc = el(lid, "AddOnCharges")
            el(aoc, "AddOnChargeType",     "Tax")
            el(aoc, "AddOnChargeSubType",  "VAT")
            el(aoc, "TaxCategory",         "Standard")
            el(aoc, "TaxRate",             f"{float(vat_rate):.2f}")
            el(aoc, "AddOnChargeAmount",   f"{vat_amt:.2f}")
            el(aoc, "CurrencyCode",        currency)

        el(lid, "TotalAddOnChargeAmount", f"{total_addon_all:.2f}" if has_vat else "0.00")

        el(li, "TotalNetAmount",          f"{amt_net:.2f}")
        el(li, "TotalAddOnChargeAmount",  f"{total_addon_all:.2f}" if has_vat else "0.00")

    # ── InvoiceSummary ───────────────────────────────────────────────────────
    total_gross = float(money(fields["total_gross"]))
    total_vat   = float(money(fields["total_vat"]))
    # Use computed VAT if extracted total_vat looks like a rate rather than an amount
    if total_vat == float(vat_rate) or total_vat == 0.0:
        total_vat = total_addon_all
    if total_gross == 0.0:
        total_gross = total_net_all + total_vat

    summary = el(invoice, "InvoiceSummary")
    el(summary, "LineItemCount",          str(len(station_lines)))
    el(summary, "TotalLineItemAmount",    f"{total_net_all:.2f}")
    el(summary, "TotalAddOnChargeAmount", f"{total_vat:.2f}")
    el(summary, "TotalAmount",            f"{total_gross:.2f}")
    el(summary, "CurrencyCode",           currency)

    # Invoice-level AddOnCharges (VAT summary at invoice level per §2.3 rule 5)
    if has_vat and total_vat > 0:
        inv_aoc = el(summary, "AddOnCharges")
        el(inv_aoc, "AddOnChargeType",    "Tax")
        el(inv_aoc, "AddOnChargeSubType", "VAT")
        el(inv_aoc, "TaxCategory",        "Standard")
        el(inv_aoc, "TaxRate",            f"{float(vat_rate):.2f}")
        el(inv_aoc, "AddOnChargeAmount",  f"{total_vat:.2f}")
        el(inv_aoc, "CurrencyCode",       currency)

    # ── TransmissionSummary ──────────────────────────────────────────────────
    ts = el(transmission, "TransmissionSummary")
    el(ts, "InvoiceCount", "1")
    ta = el(ts, "TotalAmount", f"{total_gross:.2f}")
    ta.set("CurrencyCode", currency)

    # ── Serialise ─────────────────────────────────────────────────────────────
    raw    = ET.tostring(transmission, encoding="unicode", xml_declaration=False)
    pretty = minidom.parseString(raw).toprettyxml(indent="  ")
    return pretty


# ── Flask routes ─────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PDF → IATA IS-XML</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;600&display=swap');
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #0d0d0d; --surface: #161616; --border: #2a2a2a;
    --accent: #00e5a0; --accent-dim: #00e5a018;
    --text: #e8e8e8; --muted: #666; --danger: #ff4d4d; --warn: #f5a623;
  }
  body { background: var(--bg); color: var(--text);
    font-family: 'IBM Plex Sans', sans-serif;
    min-height: 100vh; display: flex; flex-direction: column;
    align-items: center; padding: 48px 24px 80px; }
  header { width: 100%; max-width: 820px; margin-bottom: 40px;
    border-bottom: 1px solid var(--border); padding-bottom: 24px; }
  .logo { font-family: 'IBM Plex Mono', monospace; font-size: 11px;
    letter-spacing: 0.25em; color: var(--accent); text-transform: uppercase; margin-bottom: 10px; }
  h1 { font-size: 28px; font-weight: 300; letter-spacing: -0.02em; }
  h1 span { color: var(--accent); font-weight: 600; }
  .badge { display: inline-block; background: var(--accent-dim); border: 1px solid var(--accent);
    color: var(--accent); font-family: 'IBM Plex Mono', monospace; font-size: 10px;
    padding: 2px 8px; letter-spacing: 0.1em; margin-top: 8px; }
  .subtitle { font-size: 11px; color: var(--muted); margin-top: 8px; font-family: 'IBM Plex Mono', monospace; }
  main { width: 100%; max-width: 820px; }
  #dropzone { border: 1px dashed var(--border); background: var(--surface);
    padding: 52px 32px; text-align: center; cursor: pointer;
    transition: border-color 0.2s, background 0.2s; margin-bottom: 20px; }
  #dropzone.drag-over { border-color: var(--accent); background: var(--accent-dim); }
  .drop-icon { font-size: 36px; margin-bottom: 14px; display: block; }
  .drop-label { font-size: 15px; font-weight: 600; margin-bottom: 6px; }
  .drop-sub { font-size: 11px; color: var(--muted); font-family: 'IBM Plex Mono', monospace; }
  #file-input { display: none; }
  #queue { display: flex; flex-direction: column; gap: 8px; margin-bottom: 20px; }
  .file-row { background: var(--surface); border: 1px solid var(--border);
    border-left: 3px solid var(--border);
    padding: 10px 14px; display: flex; align-items: center; gap: 10px;
    font-family: 'IBM Plex Mono', monospace; font-size: 11px; }
  .file-row.done { border-left-color: var(--accent); }
  .file-row.error { border-left-color: var(--danger); }
  .file-row.processing { border-left-color: var(--warn); }
  .file-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .file-meta { color: var(--muted); flex-shrink: 0; font-size: 10px; }
  .file-status { flex-shrink: 0; font-size: 10px; min-width: 88px; text-align: right; }
  .file-status.ok { color: var(--accent); } .file-status.err { color: var(--danger); } .file-status.proc { color: var(--warn); }
  .dl-btn { background: var(--accent); color: #000; border: none;
    padding: 4px 12px; font-family: 'IBM Plex Mono', monospace;
    font-size: 10px; font-weight: 600; cursor: pointer; flex-shrink: 0; }
  .dl-btn:hover { background: #00ffb3; }
  .rm-btn { background: transparent; color: var(--muted); border: none;
    font-size: 13px; cursor: pointer; flex-shrink: 0; }
  .rm-btn:hover { color: var(--danger); }
  .actions { display: flex; gap: 10px; margin-bottom: 24px; flex-wrap: wrap; }
  .btn { padding: 11px 26px; font-family: 'IBM Plex Mono', monospace;
    font-size: 11px; font-weight: 600; letter-spacing: 0.08em;
    text-transform: uppercase; border: none; cursor: pointer; }
  .btn-primary { background: var(--accent); color: #000; }
  .btn-primary:hover { background: #00ffb3; }
  .btn-primary:disabled { background: #2a2a2a; color: #444; cursor: not-allowed; }
  .btn-ghost { background: transparent; color: var(--muted); border: 1px solid var(--border); }
  .btn-ghost:hover { color: var(--text); border-color: #555; }
  .progress-bar-wrap { height: 2px; background: var(--border); margin-bottom: 20px; }
  .progress-bar { height: 2px; background: var(--accent); width: 0%; transition: width 0.3s; }
  .status-line { font-family: 'IBM Plex Mono', monospace; font-size: 11px; color: var(--muted); margin-bottom: 20px; }
  .log-label { font-family: 'IBM Plex Mono', monospace; font-size: 10px;
    letter-spacing: 0.15em; color: var(--muted); text-transform: uppercase; margin-bottom: 6px; }
  #log { background: var(--surface); border: 1px solid var(--border);
    padding: 14px; font-family: 'IBM Plex Mono', monospace; font-size: 11px;
    color: var(--muted); max-height: 200px; overflow-y: auto; line-height: 1.9; }
  .log-ok { color: var(--accent); } .log-err { color: var(--danger); } .log-info { color: var(--warn); }
  .schema-note { margin-top: 20px; padding: 14px 16px; background: var(--surface);
    border: 1px solid var(--border); border-left: 3px solid var(--accent);
    font-family: 'IBM Plex Mono', monospace; font-size: 10px; color: var(--muted); line-height: 1.8; }
  .schema-note strong { color: var(--accent); }
</style>
</head>
<body>
<header>
  <div class="logo">Aegean Airlines · Ground Ops Tooling</div>
  <h1>PDF <span>→</span> IATA IS-XML</h1>
  <div class="badge">IS-XML Invoice Standard V3.4 · Ground Handling</div>
  <div class="subtitle">batch converter · 100% offline · no internet required</div>
</header>
<main>
  <div id="dropzone">
    <span class="drop-icon">📄</span>
    <div class="drop-label">Drop ground handler invoice PDFs here</div>
    <div class="drop-sub">or click to browse · multiple files supported</div>
    <input type="file" id="file-input" accept=".pdf" multiple>
  </div>

  <div id="queue"></div>
  <div class="progress-bar-wrap"><div class="progress-bar" id="progress"></div></div>
  <div class="status-line" id="status-line">No files loaded.</div>

  <div class="actions">
    <button class="btn btn-primary" id="convert-btn" disabled>Convert All</button>
    <button class="btn btn-ghost" id="dl-all-btn" disabled>⬇ Download All</button>
    <button class="btn btn-ghost" id="clear-btn">Clear</button>
  </div>

  <div class="log-label">Console</div>
  <div id="log"><span class="log-info">Ready — drop PDFs to begin.</span></div>

  <div class="schema-note">
    <strong>Output schema:</strong> IATA IS-XML V3.4 (full spec) &nbsp;·&nbsp;
    <strong>Charge Category:</strong> Ground Handling &nbsp;·&nbsp;
    <strong>Charge Codes:</strong> Mishandling Baggage, Baggage, Ramp Handling, Catering, Cleaning, Deicing + more<br>
    Includes: TransmissionDateTime · OrganizationDesignator · OrganizationName1 · TaxRegistrationID · CompanyRegistrationID · structured Address block · LineItemDetail · AddOnCharges (VAT per §2.3) · AttachmentIndicatorOriginal · TotalAddOnChargeAmount
  </div>
</main>

<script>
const files = [];
const dropzone   = document.getElementById('dropzone');
const fileInput  = document.getElementById('file-input');
const queueEl    = document.getElementById('queue');
const convertBtn = document.getElementById('convert-btn');
const dlAllBtn   = document.getElementById('dl-all-btn');
const clearBtn   = document.getElementById('clear-btn');
const logEl      = document.getElementById('log');
const progressEl = document.getElementById('progress');
const statusLine = document.getElementById('status-line');

function log(msg, cls='') {
  const d = document.createElement('div');
  if (cls) d.className = 'log-' + cls;
  d.textContent = msg;
  logEl.appendChild(d);
  logEl.scrollTop = logEl.scrollHeight;
}

function fmt(b) {
  return b < 1048576 ? (b/1024).toFixed(1)+' KB' : (b/1048576).toFixed(1)+' MB';
}

function updateStatus() {
  const total = files.length, done = files.filter(f=>f.status==='done').length, errs = files.filter(f=>f.status==='error').length;
  statusLine.textContent = total===0 ? 'No files loaded.' : `${total} file(s) · ${done} converted · ${errs} error(s)`;
  progressEl.style.width = total ? (done/total*100)+'%' : '0%';
  dlAllBtn.disabled = done === 0;
}

function renderQueue() {
  queueEl.innerHTML = '';
  files.forEach((f, i) => {
    const row = document.createElement('div');
    row.className = 'file-row ' + f.status;
    const statusClass = f.status==='done'?'ok':f.status==='error'?'err':f.status==='processing'?'proc':'';
    const statusText  = f.status==='pending'?'–':f.status==='processing'?'converting…':f.status==='done'?'✓ IATA XML':'✗ error';
    row.innerHTML = `
      <span class="file-name">${f.name}</span>
      <span class="file-meta">${fmt(f.size)}</span>
      ${f.chargeCode ? `<span class="file-meta" style="color:var(--accent)">${f.chargeCode}</span>` : ''}
      <span class="file-status ${statusClass}">${statusText}</span>
      ${f.status==='done' ? `<button class="dl-btn" data-i="${i}">Download XML</button>` : ''}
      <button class="rm-btn" data-rm="${i}">✕</button>
    `;
    queueEl.appendChild(row);
  });
  queueEl.querySelectorAll('.dl-btn').forEach(b => b.addEventListener('click', ()=>downloadOne(+b.dataset.i)));
  queueEl.querySelectorAll('.rm-btn').forEach(b => b.addEventListener('click', ()=>{
    files.splice(+b.dataset.rm, 1); renderQueue(); updateStatus(); convertBtn.disabled = files.length===0;
  }));
  updateStatus();
}

// Block browser from opening PDF on drop
document.addEventListener('dragover', e => e.preventDefault());
document.addEventListener('drop',     e => e.preventDefault());

dropzone.addEventListener('dragenter', e => { e.preventDefault(); e.stopPropagation(); dropzone.classList.add('drag-over'); });
dropzone.addEventListener('dragover',  e => { e.preventDefault(); e.stopPropagation(); dropzone.classList.add('drag-over'); });
dropzone.addEventListener('dragleave', e => { e.stopPropagation(); dropzone.classList.remove('drag-over'); });
dropzone.addEventListener('drop', e => {
  e.preventDefault(); e.stopPropagation();
  dropzone.classList.remove('drag-over');
  addFiles([...e.dataTransfer.files]);
});
dropzone.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', () => { addFiles([...fileInput.files]); fileInput.value=''; });

function addFiles(newFiles) {
  newFiles.filter(f => f.name.toLowerCase().endsWith('.pdf')).forEach(f => {
    if (!files.find(x => x.name===f.name && x.size===f.size))
      files.push({ file: f, name: f.name, size: f.size, status: 'pending', xmlBlob: null, chargeCode: null });
  });
  renderQueue();
  convertBtn.disabled = files.length === 0;
}

async function convertAll() {
  convertBtn.disabled = true;
  for (let i = 0; i < files.length; i++) {
    const f = files[i];
    if (f.status === 'done') continue;
    f.status = 'processing'; renderQueue();
    log(`Converting ${f.name}…`, 'info');
    try {
      const fd = new FormData();
      fd.append('file', f.file, f.name);
      const res  = await fetch('/convert', { method: 'POST', body: fd });
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || 'Server error');
      f.xmlBlob   = new Blob([data.xml], { type: 'application/xml' });
      f.chargeCode = data.charge_code;
      f.status    = 'done';
      log(`✓ ${f.name} → IATA IS-XML [${data.charge_code}] inv#${data.invoice_number}`, 'ok');
    } catch(e) {
      f.status = 'error';
      log(`✗ ${f.name}: ${e.message}`, 'err');
    }
    renderQueue();
  }
  convertBtn.disabled = false;
}

function downloadOne(i) {
  const f = files[i];
  if (!f.xmlBlob) return;
  const a = document.createElement('a');
  a.href = URL.createObjectURL(f.xmlBlob);
  a.download = f.name.replace(/\.pdf$/i, '_IATA.xml');
  a.click();
}

async function downloadAll() {
  for (let i = 0; i < files.length; i++) {
    if (files[i].status === 'done') { downloadOne(i); await new Promise(r=>setTimeout(r,400)); }
  }
}

convertBtn.addEventListener('click', convertAll);
dlAllBtn.addEventListener('click', downloadAll);
clearBtn.addEventListener('click', () => {
  files.length = 0; renderQueue(); convertBtn.disabled = true; log('Queue cleared.', 'info');
});
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/convert", methods=["POST"])
def convert():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Not a PDF"}), 400
    try:
        pdf_bytes = f.read()
        fields    = extract_invoice_fields(pdf_bytes)
        xml_str   = build_iata_xml(fields, f.filename)
        return jsonify({
            "xml":            xml_str,
            "charge_code":    fields["charge_code"],
            "invoice_number": fields["inv_number"],
            "currency":       fields["currency"],
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
