import re
import json
import argparse
from datetime import datetime
from typing import List, Dict, Tuple, Optional

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


# -----------------------------
# Helpers
# -----------------------------
JP_NUM = {"０":"0","１":"1","２":"2","３":"3","４":"4","５":"5","６":"6","７":"7","８":"8","９":"9"}


def jpn_digits_to_ascii(s: str) -> str:
    return "".join(JP_NUM.get(ch, ch) for ch in s)


def num_from_text(s: str) -> Optional[int]:
    s = jpn_digits_to_ascii(s)
    m = re.search(r"(-?\d+)", s)
    return int(m.group(1)) if m else None


def money_from_text(s: str) -> Optional[int]:
    """Return integer amount in JPY if found (commas allowed)."""
    s = jpn_digits_to_ascii(s)
    m = re.search(r"(\d{1,3}(?:,\d{3})+|\d+)\s*円", s)
    if not m:
        return None
    return int(m.group(1).replace(",", ""))


def months_from_text(s: str) -> Optional[float]:
    s = jpn_digits_to_ascii(s)
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*ヶ月", s)
    return float(m.group(1)) if m else None


def y_or_n(flag: bool) -> str:
    return "Y" if flag else "N"


def split_address(addr: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Very best-effort parsing for JP address: Prefecture / Ward-city / District / Chome-Banchi"""
    if not addr:
        return None, None, None, None
    # Prefecture is usually first token endswith 都/道/府/県
    m = re.match(r"^(.*?[都道府県])(.+)$", addr)
    if not m:
        return None, None, None, None
    prefecture, rest = m.group(1), m.group(2)
    # Ward (区) or city (市)
    m2 = re.match(r"^(.*?[市区郡])(.*)$", rest)
    if not m2:
        return prefecture, None, None, None
    city = m2.group(1)
    tail = m2.group(2)

    # District before 番/丁目/−… keep raw
    district = None
    chome_banchi = None
    # e.g. 太平一丁目１２番4号  → district: 太平, chome_banchi: 一丁目１２番4号
    m3 = re.match(r"^(.+?)(\d.*|[一二三四五六七八九十]+\s*丁目.*)$", tail.strip())
    if m3:
        district = m3.group(1).strip(" ・")
        chome_banchi = m3.group(2).strip()
    else:
        # fallback: split last whitespace
        parts = tail.strip().split()
        if len(parts) >= 2:
            district = parts[0]
            chome_banchi = " ".join(parts[1:])
        else:
            district = tail.strip()

    return prefecture, city, district, chome_banchi


def parse_line_station_walk(html_block: str) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """Parse like: 'ＪＲ 総武線 錦糸町 徒歩9分'"""
    # remove tags
    text = re.sub(r"<[^>]+>", " ", html_block or "")
    text = re.sub(r"\s+", " ", text).strip()
    # find walk
    walk = None
    mw = re.search(r"徒歩\s*([0-9０-９]+)\s*分", text)
    if mw:
        walk = num_from_text(mw.group(1))
    # line and station – take the last word before 徒歩 as station, previous token as line
    mls = re.search(r"(?:JR|ＪＲ)?\s*([^\s]+線)\s*([^\s]+)\s*徒歩", text)
    if mls:
        return mls.group(2), mls.group(1), walk
    # fallback: take two tokens before 徒歩
    toks = text.split()
    if "徒歩" in toks:
        i = toks.index("徒歩")
        if i >= 2:
            line = toks[i - 2]
            st = toks[i - 1]
            return st, line, walk
    return None, None, walk


def extract_features_map(equip_text: str) -> Dict[str, str]:
    t = equip_text or ""
    flags = {
        "autolock": "オートロック" in t,
        "delivery_box": ("宅配ロッカー" in t) or ("宅配ボックス" in t),
        "elevator": "エレベータ" in t or "エレベーター" in t,
        "balcony": "バルコニー" in t,
        "bath": ("バストイレ" in t) or ("バス有" in t),
        "washing_machine": ("室内洗濯機置場" in t),
        "underfloor_heating": ("床暖房" in t),
        "bath_water_heater": ("追い焚き" in t) or ("給湯" in t),
        "bs": ("BS" in t),
        "cable": ("CS" in t),
        "system_kitchen": ("システムキッチン" in t),
        "range": ("コンロ" in t),
        "internet_broadband": ("インターネット" in t),
    }
    return {k: y_or_n(v) for k, v in flags.items()}


def pick_lock_exchange(text: str) -> Optional[int]:
    """Find money for key/lock exchange only if present."""
    if not text:
        return None
    if re.search(r"(鍵交換|キー交換|玄関[鍵錠]交換|鍵交換費|鍵交換料)", text):
        return money_from_text(text)
    return None


def guess_building_type(structure_text: Optional[str], floors: Optional[int], bldg_name: Optional[str]) -> Optional[str]:
    st = structure_text or ""
    bn = bldg_name or ""
    if "戸建" in bn or "一戸建" in bn:
        return "戸建て"
    if "鉄筋" in st or "RC" in st or (floors is not None and floors >= 3):
        return "マンション"
    if "木造" in st or (floors is not None and floors <= 2):
        return "アパート"
    return None


def ensure_click(page, selector: str, timeout=5000):
    """Click if exists & visible; ignore otherwise."""
    try:
        el = page.locator(selector)
        if el.count() and el.first.is_visible():
            el.first.click(timeout=timeout)
            return True
    except PWTimeout:
        pass
    return False


# -----------------------------
# Scraper
# -----------------------------

def scrape(url: str, headless: bool = True) -> Dict:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)

        # ---------- HEADER ----------
        h1 = page.locator("h1.c-buildroom__summary-h").inner_text().strip()
        # e.g. プレディアコート錦糸町スカイビュー  2階２０１
        # safer: take first line as building name
        # Building name: remove trailing " <floor>階<unit>" if present (works even when on same line)
        bn = re.sub(r"\s*\d+\s*階\s*[０-９0-9]+.*$", "", h1)
        building_name_ja = bn.strip()
        floor_no = None
        unit_no = None
        m_h = re.search(r"(\d+)\s*階\s*([０-９0-9]+)", h1)
        if m_h:
            floor_no = num_from_text(m_h.group(1))
            unit_no = jpn_digits_to_ascii(m_h.group(2))

        # property_csv_id & building id
        property_csv_id = None
        bld_cd = None
        try:
            btn = page.locator("button[data-code]").first
            if btn.count():
                property_csv_id = btn.get_attribute("data-code")
                bld_cd = btn.get_attribute("data-bld_cd")
        except Exception:
            pass

        # ---------- SUMMARY BOX ----------
        def _dd_after_dt(dt_text: str) -> Optional[str]:
            dt = page.locator(f"//dt[normalize-space()='{dt_text}']")
            if dt.count() == 0:
                return None
            dd = dt.nth(0).locator("xpath=following-sibling::dd[1]")
            return dd.inner_html().strip() if dd.count() else None

        address_html = _dd_after_dt("所在地")
        address = re.sub(r"<[^>]+>", "", address_html or "").strip() if address_html else None
        prefecture, city, district, chome_banchi = split_address(address or "")

        # 交通 (first)
        access_html = _dd_after_dt("交通") or ""
        st1, line1, walk1 = parse_line_station_walk(access_html)

        # 賃料・管理費・共益費
        rent_html = _dd_after_dt("賃料・管理費・共益費") or ""
        monthly_rent = money_from_text(rent_html)
        mtn_m = re.search(r"/\s*([0-9,０-９]+円)", rent_html)
        monthly_maintenance = money_from_text(mtn_m.group(1)) if mtn_m else None

        # 敷金／礼金
        depkey_html = _dd_after_dt("敷金／礼金") or ""
        months_deposit = months_from_text(depkey_html)
        months_key = months_from_text(depkey_html.split("/")[-1]) if "/" in depkey_html else None

        # 間取り・面積
        layout_html = _dd_after_dt("間取り・面積") or ""
        room_type = None
        size = None
        m_room = re.search(r"([0-9A-Z]+[A-Z]?[\+\w]*)\s*/", layout_html)
        if m_room:
            room_type = m_room.group(1).replace("＋", "+")
        m_size = re.search(r"([0-9.]+)\s*㎡", layout_html)
        if m_size:
            size = float(m_size.group(1))

        # 竣工日 → Year only
        built_html = _dd_after_dt("竣工日") or ""
        year = None
        my = re.search(r"([0-9]{4})年", built_html)
        if my:
            year = int(my.group(1))

        # ---------- DETAIL TABLE ----------
        # 規模構造
        structure_html = None
        try:
            structure_html = _dd_after_dt("規模構造")
        except Exception:
            structure_html = None
        structure_text = re.sub(r"<[^>]+>", "", structure_html or "").strip()
        structure = None
        floors = None
        basement_floors = None
        if structure_text:
            # e.g. 鉄筋コンクリート造 地上14階建 / 地下1階
            mstruct = re.match(r"^(.+?造)", structure_text)
            if mstruct:
                structure = mstruct.group(1)
            mf = re.search(r"地上\s*([0-9０-９]+)\s*階", structure_text)
            if mf:
                floors = num_from_text(mf.group(1))
            mb = re.search(r"地下\s*([0-9０-９]+)\s*階", structure_text)
            if mb:
                basement_floors = num_from_text(mb.group(1))

        # 入居可能日 / 更新料 / 駐車場 / 方位 / その他費用 / 設備 / 備考 / 情報更新日 / 取引態様 / 保険
        available_from = re.sub(r"<[^>]+>", "", _dd_after_dt("入居可能日") or "").strip() or None
        renewal_html = _dd_after_dt("更新料") or ""
        months_renewal = months_from_text(renewal_html)
        parking_text = re.sub(r"<[^>]+>", "", _dd_after_dt("駐車場") or "").strip()
        parking_flag = y_or_n("有" in parking_text)

        facing_text = re.sub(r"<[^>]+>", "", _dd_after_dt("方位") or "").strip()
        facing_map = {
            "北": "facing_north",
            "北東": "facing_northeast",
            "東": "facing_east",
            "南東": "facing_southeast",
            "南": "facing_south",
            "南西": "facing_southwest",
            "西": "facing_west",
            "北西": "facing_northwest",
        }
        facing_flags = {v: "N" for v in facing_map.values()}
        for k, v in facing_map.items():
            if k in facing_text:
                facing_flags[v] = "Y"

        other_fee_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", _dd_after_dt("その他費用") or "").strip())
        lock_exchange = pick_lock_exchange(other_fee_text)

        equip_text = re.sub(r"<[^>]+>", "", _dd_after_dt("専有部・共用部設備") or "").strip()
        features = extract_features_map(equip_text)

        building_desc = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", _dd_after_dt("備考") or "").strip()) or None
        info_updated = re.sub(r"<[^>]+>", "", _dd_after_dt("情報更新日") or "").strip() or None

        ad_type = re.sub(r"<[^>]+>", "", _dd_after_dt("取引態様") or "").strip() or None
        fire_insurance = re.sub(r"<[^>]+>", "", _dd_after_dt("保険") or "").strip() or None

        # Guarantor modal content (company list)
        guarantor_agency_name = None
        guarantor_agency = "N"
        try:
            body = page.locator("#guarantor .c-modal-content__body").inner_text()
            companies = re.findall(r"【([^】]+)】", body)
            if companies:
                guarantor_agency_name = ", ".join(companies)
                guarantor_agency = "Y"
        except Exception:
            pass

        # Extra booleans from equipment/notes
        motorcycle_parking = y_or_n("バイク置場" in equip_text)
        aircon_flag = y_or_n(("エアコン" in equip_text) or ("ｴｱｺﾝ" in (building_desc or "")))

        # ---------- IMAGES ----------
        images: List[Tuple[str, str]] = []  # (category, url)

        # Floorplan in main image (if present)
        try:
            main_src = page.locator(".c-buildroom__summary-pics img").first.get_attribute("src")
            if main_src:
                images.append(("floorplan", main_src))
        except Exception:
            pass

        # Interior (tab: 間取り・部屋)
        ensure_click(page, "button[data-js-buildroom-slide-tab='floorplan']")
        try:
            page.wait_for_selector(".c-buildroom-slide__thumbs img", timeout=8000)
            for el in page.locator(".c-buildroom-slide__thumbs img").all():
                src = el.get_attribute("src")
                if src and ("nofloorplan.webp" not in src):
                    images.append(("interior", src))
        except PWTimeout:
            pass

        # Exterior (tab: 外観・共用部・周辺)
        if ensure_click(page, "button[data-js-buildroom-slide-tab='exterior']", timeout=8000):
            try:
                page.wait_for_selector(".c-buildroom-slide__thumbs img", timeout=12000)
                for el in page.locator(".c-buildroom-slide__thumbs img").all():
                    src = el.get_attribute("src")
                    if src and ("nofloorplan.webp" not in src):
                        images.append(("exterior", src))
            except PWTimeout:
                pass

        # Deduplicate while keeping order
        seen = set()
        uniq_imgs = []
        for cat, url_i in images:
            key = (cat, url_i)
            if url_i and key not in seen:
                uniq_imgs.append((cat, url_i))
                seen.add(key)

        # Map to image_category_1.. and image_url_1..
        image_fields = {}
        for idx, (cat, url_i) in enumerate(uniq_imgs[:16], start=1):
            image_fields[f"image_category_{idx}"] = cat
            image_fields[f"image_url_{idx}"] = url_i

        # ---------- Station lat/lng ----------
        map_lat = None
        map_lng = None

        # ---------- Try to enrich from Building page (postcode, maybe more) ----------
        postcode = None
        if bld_cd:
            try:
                bpage = context.new_page()
                bpage.goto(f"https://www.mitsui-chintai.co.jp/rf/tatemono/{bld_cd}", wait_until="domcontentloaded", timeout=60000)
                html = bpage.content()
                mzip = re.search(r"〒\s*([0-9]{3}-?[0-9]{4})", html)
                if mzip:
                    # normalize to 123-4567
                    zp = mzip.group(1)
                    if "-" not in zp:
                        postcode = f"{zp[:3]}-{zp[3:]}"
                    else:
                        postcode = zp
                bpage.close()
            except Exception:
                pass

        # ---------- Derivations ----------
        building_type = guess_building_type(structure_text, floors, building_name_ja)

        # numerics derived from months * rent (best-effort)
        numeric_deposit = int(monthly_rent * months_deposit) if (monthly_rent and months_deposit) else None
        numeric_key = int(monthly_rent * months_key) if (monthly_rent and months_key) else None
        numeric_renewal = int(monthly_rent * months_renewal) if (monthly_rent and months_renewal) else None

        # ---------- Compose result ----------
        result = {k: None for k in [
            # all keys (init to None). We fill the ones we have:
            "link","property_csv_id","postcode","prefecture","city","district","chome_banchi",
            "building_type","year","building_name_en","building_name_ja","building_name_zh_CN","building_name_zh_TW",
            "building_description_en","building_description_ja","building_description_zh_CN","building_description_zh_TW",
            "building_landmarks_en","building_landmarks_ja","building_landmarks_zh_CN","building_landmarks_zh_TW",
            "station_name_1","train_line_name_1","walk_1","bus_1","car_1","cycle_1",
            "station_name_2","train_line_name_2","walk_2","bus_2","car_2","cycle_2",
            "station_name_3","train_line_name_3","walk_3","bus_3","car_3","cycle_3",
            "station_name_4","train_line_name_4","walk_4","bus_4","car_4","cycle_4",
            "station_name_5","train_line_name_5","walk_5","bus_5","car_5","cycle_5",
            "map_lat","map_lng","num_units","floors","basement_floors","parking","parking_cost",
            "bicycle_parking","motorcycle_parking","structure","building_notes","building_style",
            "autolock","credit_card","concierge","delivery_box","elevator","gym","newly_built","pets",
            "swimming_pool","ur","room_type","size","unit_no","ad_type","available_from",
            "property_description_en","property_description_ja","property_description_zh_CN","property_description_zh_TW",
            "property_other_expenses_en","property_other_expenses_ja","property_other_expenses_zh_CN","property_other_expenses_zh_TW",
            "featured_a","featured_b","featured_c","floor_no","monthly_rent","monthly_maintenance",
            "months_deposit","numeric_deposit","months_key","numeric_key","months_guarantor","numeric_guarantor",
            "months_agency","numeric_agency","months_renewal","numeric_renewal","months_deposit_amortization","numeric_deposit_amortization",
            "months_security_deposit","numeric_security_deposit","lock_exchange","fire_insurance","other_initial_fees",
            "other_subscription_fees","no_guarantor","guarantor_agency","guarantor_agency_name","rent_negotiable",
            "renewal_new_rent","lease_date","lease_months","lease_type","short_term_ok","balcony_size","property_notes",
            "facing_north","facing_northeast","facing_east","facing_southeast","facing_south","facing_southwest","facing_west","facing_northwest",
            "aircon","aircon_heater","all_electric","auto_fill_bath","balcony","bath","bath_water_heater","blinds",
            "bs","cable","carpet","cleaning_service","counter_kitchen","dishwasher","drapes","female_only","fireplace",
            "flooring","full_kitchen","furnished","gas","induction_cooker","internet_broadband","internet_wifi",
            "japanese_toilet","linen","loft","microwave","oven","phoneline","range","refrigerator","refrigerator_freezer",
            "roof_balcony","separate_toilet","shower","soho","storage","student_friendly","system_kitchen","tatami",
            "underfloor_heating","unit_bath","utensils_cutlery","veranda","washer_dryer","washing_machine","washlet",
            "western_toilet","yard","youtube","vr_link","numeric_guarantor_max","discount","create_date"
        ]}

        # Fill fields we have
        result.update({
            "link": url,
            "property_csv_id": property_csv_id,
            "postcode": postcode,
            "building_name_ja": building_name_ja,
            "floor_no": floor_no,
            "unit_no": unit_no,
            "prefecture": prefecture,
            "city": city,
            "district": district,
            "chome_banchi": chome_banchi,
            "year": year,
            "station_name_1": st1,
            "train_line_name_1": line1,
            "walk_1": walk1,
            "monthly_rent": monthly_rent,
            "monthly_maintenance": monthly_maintenance,
            "months_deposit": months_deposit,
            "months_key": months_key,
            "room_type": room_type,
            "size": size,
            "structure": structure,
            "floors": floors,
            "basement_floors": basement_floors,
            "parking": parking_flag,
            "available_from": available_from,
            "months_renewal": months_renewal,
            "map_lat": map_lat,
            "map_lng": map_lng,
            "building_description_ja": building_desc,
            "building_notes": building_desc,
            "property_notes": building_desc,  # keep same as note field per guideline
            "property_other_expenses_ja": other_fee_text,
            "other_initial_fees": other_fee_text,  # map as initial fees best-effort
            "lock_exchange": lock_exchange,
            "ad_type": ad_type,
            "fire_insurance": fire_insurance,
            "guarantor_agency": guarantor_agency,
            "guarantor_agency_name": guarantor_agency_name,
            "no_guarantor": y_or_n(False),
            # facing flags
            **facing_flags,
            # features to flags
            **features,
            # some obvious derived toggles/flags
            "balcony": features.get("balcony", "N"),
            "bath": features.get("bath", "N"),
            "washing_machine": features.get("washing_machine", "N"),
            "underfloor_heating": features.get("underfloor_heating", "N"),
            "bath_water_heater": features.get("bath_water_heater", "N"),
            "bs": features.get("bs", "N"),
            "cable": features.get("cable", "N"),
            "system_kitchen": features.get("system_kitchen", "N"),
            "range": features.get("range", "N"),
            "internet_broadband": features.get("internet_broadband", "N"),
            "autolock": features.get("autolock", "N"),
            "delivery_box": features.get("delivery_box", "N"),
            "elevator": features.get("elevator", "N"),
            "aircon": aircon_flag,
            "motorcycle_parking": motorcycle_parking,
            "building_type": building_type,
            # renewal based on new rent wording
            "renewal_new_rent": y_or_n(True) if months_renewal else None,
            # derived numerics
            "numeric_deposit": numeric_deposit,
            "numeric_key": numeric_key,
            "numeric_renewal": numeric_renewal,
            "create_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

        # mark newly built if "新築" flag shown near header
        try:
            new_flag = "新築" in page.locator(".c-buildroom__summary-flag").inner_text()
            result["newly_built"] = y_or_n(new_flag)
        except Exception:
            result["newly_built"] = "N"

        # Images
        result.update(image_fields)

        browser.close()
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="Property URL")
    parser.add_argument("--headful", action="store_true", help="Run with browser UI")
    args = parser.parse_args()

    data = scrape(args.url, headless=not args.headful)
    print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
