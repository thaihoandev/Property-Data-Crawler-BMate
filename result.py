import re
import json
import argparse
import contextlib
import io
from datetime import datetime
from typing import List, Dict, Tuple, Optional
import time
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
    """Best-effort parsing for JP address: Prefecture / Ward-city / District / Chome-Banchi"""
    if not addr:
        return None, None, None, None
    m = re.match(r"^(.*?[都道府県])(.+)$", addr)
    if not m:
        return None, None, None, None
    prefecture, rest = m.group(1), m.group(2)
    m2 = re.match(r"^(.*?[市区郡])(.*)$", rest)
    if not m2:
        return prefecture, None, None, None
    city = m2.group(1)
    tail = m2.group(2)
    district = None
    chome_banchi = None
    m3 = re.match(r"^(.+?)(\d.*|[一二三四五六七八九十]+\s*丁目.*)$", tail.strip())
    if m3:
        district = m3.group(1).strip(" ・")
        chome_banchi = m3.group(2).strip()
    else:
        parts = tail.strip().split()
        if len(parts) >= 2:
            district = parts[0]
            chome_banchi = " ".join(parts[1:])
        else:
            district = tail.strip()
    return prefecture, city, district, chome_banchi

def parse_line_station_walk(html_block: str) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """Parse phrase like: 'ＪＲ 総武線 錦糸町 徒歩9分'"""
    text = re.sub(r"<[^>]+>", " ", html_block or "")
    text = re.sub(r"\s+", " ", text).strip()
    walk = None
    mw = re.search(r"徒歩\s*([0-9０-９]+)\s*分", text)
    if mw:
        walk = num_from_text(mw.group(1))
    mls = re.search(r"(?:JR|ＪＲ)?\s*([^\s]+線)\s*([^\s]+)\s*徒歩", text)
    if mls:
        return mls.group(2), mls.group(1), walk
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
    """Find key/lock exchange fee only if present in the text."""
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

def wait_for_network_idle(page, timeout_ms: int = 5000):
    """Wait for network to be idle (no requests for 500ms)"""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PWTimeout:
        pass

def activate_tab_and_wait_images_improved(page, tab: str, timeout_ms: int = 10000) -> bool:
    """
    Enhanced version with multiple strategies to ensure AJAX content loads
    """
    try:
        print(f"Activating {tab} tab...")
        thumbs_wrapper = page.locator(".c-buildroom-slide__thumbs .swiper-wrapper").first
        initial_count = 0
        try:
            initial_count = thumbs_wrapper.locator("img").count()
        except Exception:
            pass
        tab_button = page.locator(f"button[data-js-buildroom-slide-tab='{tab}']").first
        if not tab_button or tab_button.count() == 0:
            print(f"Tab button for {tab} not found")
            return False
        try:
            tab_button.scroll_into_view_if_needed(timeout=3000)
            tab_button.wait_for(state="visible", timeout=3000)
        except PWTimeout:
            print(f"Tab button for {tab} not visible")
            pass
        try:
            tab_button.click(timeout=5000)
            print(f"Clicked {tab} tab button")
        except PWTimeout:
            print(f"Failed to click {tab} tab button")
            return False
        time.sleep(0.5)
        try:
            page.wait_for_function(
                """(button) => {
                    return button && button.classList.contains('--is-active');
                }""",
                arg=tab_button.element_handle(),
                timeout=3000
            )
            print(f"{tab} tab became active")
        except PWTimeout:
            print(f"{tab} tab did not become active (might still work)")
            pass
        wait_for_network_idle(page, 3000)
        max_attempts = 5
        for attempt in range(max_attempts):
            try:
                current_count = thumbs_wrapper.locator("img").count()
                images = thumbs_wrapper.locator("img").all()
                valid_images = []
                for img in images:
                    try:
                        src = img.get_attribute("src")
                        if src and "nofloorplan.webp" not in src:
                            valid_images.append(src)
                    except Exception:
                        continue
                if valid_images:
                    print(f"Found {len(valid_images)} valid images in {tab} tab")
                    return True
                if attempt < max_attempts - 1:
                    print(f"Attempt {attempt + 1}: Waiting for {tab} images to load...")
                    time.sleep(1)
            except Exception as e:
                print(f"Error checking images on attempt {attempt + 1}: {e}")
                if attempt < max_attempts - 1:
                    time.sleep(1)
        try:
            main_images = page.locator(".c-buildroom__summary-pics img").all()
            for img in main_images:
                try:
                    src = img.get_attribute("src")
                    if src and "nofloorplan.webp" not in src:
                        print(f"Found image in main slide: {src}")
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        print(f"No valid images found for {tab} tab")
        return False
    except Exception as e:
        print(f"Error in activate_tab_and_wait_images_improved for {tab}: {e}")
        return False

def collect_current_imgs_improved(page) -> List[str]:
    """
    Enhanced image collection with better error handling, restricted to c-buildroom
    """
    urls = set()
    # Restrict selectors to c-buildroom scope only
    selectors = [
        ".c-buildroom .c-buildroom-slide__thumbs img",
        ".c-buildroom .c-buildroom__summary-pics img",
        ".c-buildroom .c-buildroom-slide__main img"
    ]
    for selector in selectors:
        try:
            images = page.locator(selector).all()
            print(f"Found {len(images)} images with selector: {selector}")
            for img in images:
                try:
                    src = img.get_attribute("src")
                    if src and "nofloorplan.webp" not in src and src.startswith("http"):
                        urls.add(src)
                        print(f"Added image URL: {src}")
                except Exception as e:
                    print(f"Error getting src from image: {e}")
                    continue
        except Exception as e:
            print(f"Error with selector {selector}: {e}")
            continue
    print(f"Total unique image URLs collected: {len(urls)}")
    return list(urls)

def is_floorplan_url(src: str) -> bool:
    """
    Heuristic: on this site floorplan images often end with '...c.jpg' under /rf/resized/ path
    or contain the word 'floorplan' in URL.
    """
    if not src:
        return False
    if "floorplan" in src.lower():
        return True
    return bool(re.search(r"/resized/[^/]+/[^/]*c\.jpg$", src))

# -----------------------------
# Scraper with improved image handling
# -----------------------------

def scrape(url: str, headless: bool = True) -> Dict:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        def log_request(request):
            if "ajax" in request.url.lower() or "api" in request.url.lower():
                print(f"AJAX Request: {request.url}")
        def log_response(response):
            if "ajax" in response.url.lower() or "api" in response.url.lower():
                print(f"AJAX Response: {response.url} - Status: {response.status}")
        page = context.new_page()
        page.on("request", log_request)
        page.on("response", log_response)
        print(f"Loading page: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(2)
        h1 = page.locator("h1.c-buildroom__summary-h").inner_text().strip()
        bn = re.sub(r"\s*\d+\s*階\s*[０-９0-9]+.*$", "", h1)
        building_name_ja = bn.strip()
        floor_no = None
        unit_no = None
        m_h = re.search(r"(\d+)\s*階\s*([０-９0-9]+)", h1)
        if m_h:
            floor_no = num_from_text(m_h.group(1))
            unit_no = jpn_digits_to_ascii(m_h.group(2))
        property_csv_id = None
        bld_cd = None
        try:
            btn = page.locator("button[data-code]").first
            if btn.count():
                property_csv_id = btn.get_attribute("data-code")
                bld_cd = btn.get_attribute("data-bld_cd")
        except Exception:
            pass
        def _dd_after_dt(dt_text: str) -> Optional[str]:
            dt = page.locator(f"//dt[normalize-space()='{dt_text}']")
            if dt.count() == 0:
                return None
            dd = dt.nth(0).locator("xpath=following-sibling::dd[1]")
            return dd.inner_html().strip() if dd.count() else None
        address_html = _dd_after_dt("所在地")
        address = re.sub(r"<[^>]+>", "", address_html or "").strip() if address_html else None
        prefecture, city, district, chome_banchi = split_address(address or "")
        access_html = _dd_after_dt("交通") or ""
        st1, line1, walk1 = parse_line_station_walk(access_html)
        rent_html = _dd_after_dt("賃料・管理費・共益費") or ""
        monthly_rent = money_from_text(rent_html)
        mtn_m = re.search(r"/\s*([0-9,０-９]+円)", rent_html)
        monthly_maintenance = money_from_text(mtn_m.group(1)) if mtn_m else None
        depkey_html = _dd_after_dt("敷金／礼金") or ""
        months_deposit = months_from_text(depkey_html)
        months_key = months_from_text(depkey_html.split("/")[-1]) if "/" in depkey_html else None
        layout_html = _dd_after_dt("間取り・面積") or ""
        room_type = None
        size = None
        m_room = re.search(r"([0-9A-Z]+[A-Z]?[\+\w]*)\s*/", layout_html)
        if m_room:
            room_type = m_room.group(1).replace("＋", "+")
        m_size = re.search(r"([0-9.]+)\s*㎡", layout_html)
        if m_size:
            size = float(m_size.group(1))
        built_html = _dd_after_dt("竣工日") or ""
        year = None
        my = re.search(r"([0-9]{4})年", built_html)
        if my:
            year = int(my.group(1))
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
            mstruct = re.match(r"^(.+?造)", structure_text)
            if mstruct:
                structure = mstruct.group(1)
            mf = re.search(r"地上\s*([0-9０-９]+)\s*階", structure_text)
            if mf:
                floors = num_from_text(mf.group(1))
            mb = re.search(r"地下\s*([0-9０-９]+)\s*階", structure_text)
            if mb:
                basement_floors = num_from_text(mb.group(1))
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
        motorcycle_parking = y_or_n("バイク置場" in equip_text)
        aircon_flag = y_or_n(("エアコン" in equip_text) or ("ｴｱｺﾝ" in (building_desc or "")))
        print("Starting image collection...")
        cat_priority = {"exterior": 0, "interior": 1, "floorplan": 2}
        url_index: Dict[str, int] = {}
        final_images: List[Tuple[str, str]] = []
        print("Collecting floorplan images...")
        floorplan_urls: List[str] = []
        if activate_tab_and_wait_images_improved(page, "floorplan"):
            floorplan_urls = collect_current_imgs_improved(page)
            print(f"Found {len(floorplan_urls)} floorplan URLs")
        for src in floorplan_urls:
            cat = "floorplan" if is_floorplan_url(src) else "interior"
            if src in url_index:
                idx = url_index[src]
                if cat_priority[cat] > cat_priority[final_images[idx][0]]:
                    final_images[idx] = (cat, src)
            else:
                url_index[src] = len(final_images)
                final_images.append((cat, src))
        print("Collecting exterior images...")
        exterior_urls: List[str] = []
        if activate_tab_and_wait_images_improved(page, "exterior"):
            exterior_urls = collect_current_imgs_improved(page)
            print(f"Found {len(exterior_urls)} exterior URLs")
        for src in exterior_urls:
            if src in url_index:
                continue
            url_index[src] = len(final_images)
            final_images.append(("exterior", src))
        print("Fallback image collection...")
        try:
            fallback_urls = collect_current_imgs_improved(page)
            for src in fallback_urls:
                if src not in url_index:
                    cat = "floorplan" if is_floorplan_url(src) else "interior"
                    url_index[src] = len(final_images)
                    final_images.append((cat, src))
        except Exception as e:
            print(f"Error in fallback collection: {e}")
        print(f"Total images collected: {len(final_images)}")
        for i, (cat, url_i) in enumerate(final_images):
            print(f"  {i+1}: {cat} - {url_i}")
        image_fields = {}
        for idx, (cat, url_i) in enumerate(final_images[:16], start=1):
            image_fields[f"image_category_{idx}"] = cat
            image_fields[f"image_url_{idx}"] = url_i
        map_lat = None
        map_lng = None
        postcode = None
        if bld_cd:
            try:
                bpage = context.new_page()
                bpage.goto(f"https://www.mitsui-chintai.co.jp/rf/tatemono/{bld_cd}", wait_until="domcontentloaded", timeout=60000)
                html = bpage.content()
                mzip = re.search(r"〒\s*([0-9]{3}-?[0-9]{4})", html)
                if mzip:
                    zp = mzip.group(1)
                    if "-" not in zp:
                        postcode = f"{zp[:3]}-{zp[3:]}"
                    else:
                        postcode = zp
                bpage.close()
            except Exception:
                pass
        building_type = guess_building_type(structure_text, floors, building_name_ja)
        numeric_deposit = int(monthly_rent * months_deposit) if (monthly_rent and months_deposit) else None
        numeric_key = int(monthly_rent * months_key) if (monthly_rent and months_key) else None
        numeric_renewal = int(monthly_rent * months_renewal) if (monthly_rent and months_renewal) else None
        result = {k: None for k in [
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
            "property_notes": building_desc,
            "property_other_expenses_ja": other_fee_text,
            "other_initial_fees": other_fee_text,
            "lock_exchange": lock_exchange,
            "ad_type": ad_type,
            "fire_insurance": fire_insurance,
            "guarantor_agency": guarantor_agency,
            "guarantor_agency_name": guarantor_agency_name,
            "no_guarantor": y_or_n(False),
            **facing_flags,
            **features,
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
            "renewal_new_rent": y_or_n(True) if months_renewal else None,
            "numeric_deposit": numeric_deposit,
            "numeric_key": numeric_key,
            "numeric_renewal": numeric_renewal,
            "create_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        try:
            new_flag = "新築" in page.locator(".c-buildroom__summary-flag").inner_text()
            result["newly_built"] = y_or_n(new_flag)
        except Exception:
            result["newly_built"] = "N"
        result.update(image_fields)
        browser.close()
        return result

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="Property URL")
    parser.add_argument("--headful", action="store_true", help="Run with browser UI")
    parser.add_argument("--verbose", action="store_true", help="Show internal logs")
    args = parser.parse_args()

    # Khi không verbose: ẩn toàn bộ log trong quá trình scrape
    if args.verbose:
        data = scrape(args.url, headless=not args.headful)
    else:
        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf):
            data = scrape(args.url, headless=not args.headful)

    # Chỉ in ra JSON cuối cùng
    print(json.dumps(data, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()