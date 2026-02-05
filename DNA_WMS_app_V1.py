import streamlit as st
import pandas as pd
import gspread
from datetime import datetime
import os
import io
import json

# --- Library สำหรับอ่าน Barcode ---
from PIL import Image
from pyzbar.pyzbar import decode

# --- Library สำหรับ Google Drive (เพิ่มเข้ามา) ---
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from googleapiclient.errors import HttpError

# ==========================================
# 0. CONFIGURATION
# ==========================================
# ⚠️⚠️ ใส่ ID ของ Folder "D.NA_WMS_V01/picture" ตรงนี้ ⚠️⚠️
PICTURE_FOLDER_ID = '1i7lWnQy3iV5uodGdDsUrX6wwbyPiH6Hv' # <--- เปลี่ยนเป็น ID จริงของคุณ

st.set_page_config(page_title="WMS System", page_icon="📦")

# ==========================================
# 1. AUTHENTICATION (SHEET + DRIVE)
# ==========================================

# 1.1 เชื่อมต่อ Google Sheets (Service Account เดิม)
@st.cache_resource
def init_connection():
    try:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        json_path = os.path.join(current_dir, 'service_account.json')
        
        gc = gspread.service_account(filename=json_path)
        sh_wms = gc.open("WMS_Database")
        try:
            sh_master = gc.open("Master_Data")
        except:
            st.error("⚠️ ไม่พบไฟล์ 'Master_Data'")
            st.stop()

        return sh_wms, sh_master
    except Exception as e:
        st.error(f"เกิดข้อผิดพลาดในการเชื่อมต่อ Sheet: {e}")
        st.stop()

# 1.2 เชื่อมต่อ Google Drive (OAuth จากโค้ดตัวอย่าง)
def get_drive_credentials():
    try:
        if "oauth" in st.secrets:
            info = st.secrets["oauth"]
            creds = Credentials(
                None,
                refresh_token=info["refresh_token"],
                token_uri="https://oauth2.googleapis.com/token",
                client_id=info["client_id"],
                client_secret=info["client_secret"],
                scopes=["https://www.googleapis.com/auth/drive"]
            )
            return creds
        else:
            # Fallback: ถ้าไม่มี OAuth ลองใช้ Service Account เดียวกับ Sheet (ถ้าแชร์สิทธิ์ไว้)
            # แต่เบื้องต้น return None เพื่อแจ้งเตือน user
            return None
    except Exception as e:
        st.error(f"❌ Error Credentials: {e}")
        return None

def authenticate_drive():
    try:
        creds = get_drive_credentials()
        if creds: 
            return build('drive', 'v3', credentials=creds)
        return None
    except Exception as e:
        st.error(f"Error Drive Init: {e}")
        return None

# 1.3 ฟังก์ชัน Upload รูป
def upload_photo_to_drive(service, file_obj, filename, folder_id):
    try:
        file_metadata = {'name': filename, 'parents': [folder_id]}
        
        if isinstance(file_obj, bytes): 
            media_body = io.BytesIO(file_obj)
        else: 
            media_body = file_obj 
            
        media = MediaIoBaseUpload(media_body, mimetype='image/jpeg', chunksize=1024*1024, resumable=True)
        
        file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        return file.get('id')

    except HttpError as error:
        error_reason = json.loads(error.content.decode('utf-8'))
        st.error(f"Google Drive Error: {error_reason}")
        raise error
    except Exception as e:
        raise e

# --- INIT SHEETS ---
sh_wms, sh_master = init_connection()
ws_stock = sh_wms.worksheet("Current_Stock")
ws_log = sh_wms.worksheet("Transaction_Log")
ws_item_master = sh_master.worksheet("Item_Master")

try:
    ws_loc_master = sh_master.worksheet("Location_Master")
except:
    ws_loc_master = None

# ==========================================
# 2. HELPER FUNCTIONS
# ==========================================
def decode_barcode_from_image(image_file):
    try:
        image = Image.open(image_file)
        decoded_objects = decode(image)
        if decoded_objects:
            return decoded_objects[0].data.decode("utf-8")
        return None
    except:
        return None

def safe_get_data(worksheet):
    all_values = worksheet.get_all_values()
    if len(all_values) > 1:
        return pd.DataFrame(all_values[1:], columns=all_values[0])
    return pd.DataFrame()

@st.cache_data(ttl=300)
def get_location_map():
    loc_map = {}
    if ws_loc_master:
        data = ws_loc_master.get_all_values()
        if len(data) > 1:
            for row in data[1:]:
                if len(row) >= 6: 
                    loc_id = str(row[0]).strip()
                    loc_type = str(row[5]).strip().upper()
                    loc_map[loc_id] = loc_type
    return loc_map

def validate_move_rule(target_loc, loc_map, df_current_stock):
    if target_loc not in loc_map:
        return False, f"❌ ไม่พบ Location: '{target_loc}' ในระบบ"
    loc_type = loc_map[target_loc]
    if loc_type == "RESERVE":
        if not df_current_stock.empty:
            is_occupied = not df_current_stock[df_current_stock['Location'] == target_loc].empty
            if is_occupied:
                return False, f"❌ Location '{target_loc}' (RESERVE) มีของวางอยู่แล้ว!"
    return True, "OK"

def log_transaction(action, item_id, qty, from_loc, to_loc):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ws_log.append_row([timestamp, action, item_id, qty, from_loc, to_loc, "Admin"])

# ==========================================
# 3. UI & MENU
# ==========================================
st.title("📦 WMS: Warehouse Management")

menu = st.sidebar.radio("เมนูการทำงาน", 
    ["1. Receive (รับของ)", 
     "2. Put Away (เก็บเข้าชั้น)", 
     "3. Replenishment (เติมสินค้า)",
     "4. Picking (หยิบสินค้า)",
     "5. Ship Out (ขนส่ง)",
     "6. Add New Item (เพิ่มสินค้าใหม่)"]
)

# --- 4. ฟังก์ชันช่วยบันทึก Log ---
def log_transaction(action, item_id, qty, from_loc, to_loc):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ws_log.append_row([timestamp, action, item_id, qty, from_loc, to_loc, "Admin"])

# ==========================================
# 1. RECEIVE (แก้ไข V2 - เพิ่มกล่อง Container)
# ==========================================
if menu == "1. Receive (รับของ)":
    st.header("📥 1. Receive (V2 Updated)")  # สังเกตตรงนี้ ถ้าขึ้น V2 แปลว่า Code ใหม่มาแล้ว
    
    @st.cache_data(ttl=600)
    def load_master_data():
        return pd.DataFrame(ws_item_master.get_all_records())
    
    df_master = load_master_data()
    if not df_master.empty: df_master['Barcode'] = df_master['Barcode'].astype(str)
    
    df_stock_history = safe_get_data(ws_stock)

    if 'cam_reset_id' not in st.session_state: st.session_state.cam_reset_id = 0
    if 'scanned_code' not in st.session_state: st.session_state.scanned_code = None

    st.subheader("📍 Step 1: ระบุสินค้า")
    t1, t2 = st.tabs(["📸 กล้อง", "⌨️ พิมพ์"])
    with t1:
        c = st.camera_input("Scan", key=f"bc_{st.session_state.cam_reset_id}")
        if c:
            cd = decode_barcode_from_image(c)
            if cd: st.session_state.scanned_code = cd
    with t2:
        mi_input = st.text_input("Key", key=f"mi_{st.session_state.cam_reset_id}")
        if mi_input: st.session_state.scanned_code = mi_input

    if st.session_state.scanned_code:
        sb = st.session_state.scanned_code
        # ค้นหาข้อมูล Master
        mi = df_master[df_master['Barcode'] == str(sb)]
        st.divider()
        
        if not mi.empty:
            inf = mi.iloc[0]['Description']
            drv = 1 # ค่า Default Replen Point
            
            # ดึงค่า Replen Point ล่าสุดจากประวัติ (ถ้ามี)
            if not df_stock_history.empty and 'Item_ID' in df_stock_history.columns:
                df_stock_history['Item_ID'] = df_stock_history['Item_ID'].astype(str)
                hm = df_stock_history[df_stock_history['Item_ID'] == str(sb)]
                if not hm.empty:
                    try: drv = int(hm.iloc[-1]['Replen_Point'])
                    except: pass
            
            st.success(f"✅ **{inf}**")
            
            # ปุ่ม Cancel
            if st.button("❌ Cancel"): 
                st.session_state.scanned_code = None
                st.session_state.cam_reset_id += 1
                st.rerun()
            
            # --- FORM เริ่มต้นตรงนี้ ---
            with st.form("rf"):
                st.text_input("Code", value=sb, disabled=True)
                
                # >>> ส่วนที่เพิ่ม: กล่อง Container <<<
                container_id = st.text_input("ระบุหมายเลข Container / พาเลท", key="cont_input_new")
                # >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
                
                c1, c2 = st.columns(2)
                with c1: q = st.number_input("Qty", min_value=1, value=1)
                with c2: r = st.number_input("Replen Point", min_value=0, value=drv)
                
                if st.form_submit_button("✅ Save"):
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    
                    # เช็คค่า Container (ถ้าไม่กรอก ให้เป็น "-")
                    cont_val = container_id if container_id else "-"
                    
                    try:
                        # เรียง Data ตาม Column ใน Google Sheet
                        # Col 1:ID, 2:Name, 3:Qty, 4:Loc, 5:Status, 6:Container, 7:Replen, 8:Time
                        new_row = [str(sb), inf, q, "DOCK_IN", "Pending Putaway", cont_val, r, ts]
                        
                        ws_stock.append_row(new_row)
                        log_transaction("RECEIVE", sb, q, "-", "DOCK_IN")
                        
                        st.success(f"บันทึกสำเร็จ! (Container: {cont_val})")
                        st.session_state.scanned_code = None
                        st.session_state.cam_reset_id += 1
                        st.rerun()
                        
                    except Exception as e: 
                        st.error(f"Error: {e}")
        else: 
            st.error(f"❌ ไม่พบสินค้า Code: {sb} ใน Master Data")

# ==========================================
# 2. PUT AWAY
# ==========================================
elif menu == "2. Put Away (เก็บเข้าชั้น)":
    st.header("🏗️ 2. Put Away")
    # (Code V21)
    df = safe_get_data(ws_stock)
    loc_map = get_location_map()
    pending = df[df['Location'] == "DOCK_IN"] if not df.empty else pd.DataFrame()
    if not pending.empty:
        st.dataframe(pending[['Item_ID', 'Item_Name', 'Qty', 'Location']])
        if 'pa_r' not in st.session_state: st.session_state.pa_r = 0
        if 'pa_s' not in st.session_state: st.session_state.pa_s = None
        if st.session_state.pa_s is None:
            st.subheader("📲 Step 1: สแกนสินค้า")
            t1, t2 = st.tabs(["📸 กล้อง", "⌨️ พิมพ์"])
            with t1:
                c = st.camera_input("Scan", key=f"pc_{st.session_state.pa_r}")
                if c:
                    cd = decode_barcode_from_image(c)
                    if cd: st.session_state.pa_s = cd; st.rerun()
            with t2:
                m = st.text_input("Key", key=f"pm_{st.session_state.pa_r}")
                if m: st.session_state.pa_s = m; st.rerun()
        else:
            sel = st.session_state.pa_s
            pending['Item_ID'] = pending['Item_ID'].astype(str)
            m_row = pending[pending['Item_ID'] == str(sel)]
            if not m_row.empty:
                st.success(f"✅ Selected: {m_row.iloc[0]['Item_Name']}")
                if st.button("Cancel"): st.session_state.pa_s = None; st.session_state.pa_r += 1; st.rerun()
                st.subheader("📍 Step 2: ปลายทาง")
                t3, t4 = st.tabs(["📸 กล้อง", "⌨️ พิมพ์"])
                tgt = None
                with t3:
                    lc = st.camera_input("Loc", key=f"lc_{st.session_state.pa_r}")
                    if lc: 
                        lcd = decode_barcode_from_image(lc)
                        if lcd: tgt = lcd
                with t4:
                    lm = st.text_input("Loc Key", key=f"lm_{st.session_state.pa_r}")
                    if lm: tgt = lm
                if tgt:
                    valid, msg = validate_move_rule(tgt, loc_map, df)
                    if valid:
                        if st.button(f"Move to {tgt}", type="primary"):
                            cl = ws_stock.findall(str(sel))
                            fnd = False
                            for c in cl:
                                if ws_stock.cell(c.row, 4).value == "DOCK_IN":
                                    ws_stock.update_cell(c.row, 4, tgt)
                                    ws_stock.update_cell(c.row, 5, "Available")
                                    ws_stock.update_cell(c.row, 8, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                                    log_transaction("PUT_AWAY", sel, "All", "DOCK_IN", tgt)
                                    st.toast("Done"); st.session_state.pa_s = None; st.session_state.pa_r += 1; st.rerun()
                                    fnd = True; break
                            if not fnd: st.error("Not found")
                    else: st.error(msg)
            else: st.error("Not Found"); st.session_state.pa_s = None; st.session_state.pa_r += 1; st.rerun()
    else: st.info("No Pending Items")

# ==========================================
# 3. REPLENISHMENT (GUARD ADDED)
# ==========================================
elif menu == "3. Replenishment (เติมสินค้า)":
    st.header("🔄 3. Replenishment")
    df = safe_get_data(ws_stock)
    loc_map = get_location_map()
    if not df.empty:
        df['Qty'] = pd.to_numeric(df['Qty'], errors='coerce')
        df['Replen_Point'] = pd.to_numeric(df['Replen_Point'], errors='coerce')
        df['Loc_Type'] = df['Location'].map(loc_map)
        queue = df[(df['Qty'] <= df['Replen_Point']) & (df['Loc_Type'] == 'PICK')]
        
        if not queue.empty:
            st.error(f"🚨 ต้องเติม: {len(queue)} รายการ")
            st.dataframe(queue[['Item_ID', 'Item_Name', 'Location', 'Qty', 'Replen_Point']], hide_index=True)
            st.divider()
            opts = queue.apply(lambda x: f"{x['Item_ID']} : {x['Item_Name']} ({x['Location']})", axis=1).tolist()
            sel_task = st.selectbox("เลือกรายการ", opts)
            if sel_task:
                i_id = sel_task.split(" : ")[0]
                t_loc = sel_task.split("(")[1].replace(")", "")
                t_dat = queue[(queue['Item_ID'] == i_id) & (queue['Location'] == t_loc)].iloc[0]
                
                res_stock = df[(df['Item_ID'] == i_id) & (df['Loc_Type'] == 'RESERVE')]
                if not res_stock.empty:
                    st.success(f"พบ Reserve: {len(res_stock)} จุด")
                    st.dataframe(res_stock[['Location', 'Qty']], hide_index=True)
                    with st.form("exe_rep"):
                        c1, c2 = st.columns(2)
                        with c1: 
                            src = st.selectbox("จาก Reserve", res_stock['Location'].tolist())
                            # --- GUARD: หาจำนวนที่มีจริงใน Location ที่เลือก ---
                            max_avail = int(res_stock[res_stock['Location'] == src].iloc[0]['Qty'])
                            st.caption(f"📍 มีของ: {max_avail} ชิ้น")
                            
                        with c2: 
                            sug = int(t_dat['Replen_Point'] - t_dat['Qty'])
                            if sug > max_avail: sug = max_avail # ปรับ Suggest ไม่ให้เกินของที่มี
                            
                            # --- GUARD: ล็อค Max Value ที่หน้าจอ ---
                            qty = st.number_input("จำนวนเติม", min_value=1, max_value=max_avail, value=sug if sug > 0 else 1)
                        
                        new_rp = st.number_input("แก้ไข Replen Point", 0, value=int(t_dat['Replen_Point']))
                        
                        if st.form_submit_button("Confirm"):
                            # --- GUARD: เช็คอีกรอบก่อนบันทึก ---
                            if qty > max_avail:
                                st.error(f"❌ ทำรายการไม่ได้! คุณกรอก {qty} แต่มีของแค่ {max_avail}")
                                st.stop()

                            try:
                                # Cut Source
                                cl_s = ws_stock.findall(str(i_id))
                                for c in cl_s:
                                    if ws_stock.cell(c.row, 4).value == src:
                                        curr = int(ws_stock.cell(c.row, 3).value)
                                        if curr >= qty:
                                            rem = curr - qty
                                            if rem == 0: ws_stock.delete_rows(c.row)
                                            else: ws_stock.update_cell(c.row, 3, rem)
                                            break
                                # Add Target
                                cl_t = ws_stock.findall(str(i_id))
                                for c in cl_t:
                                    if ws_stock.cell(c.row, 4).value == t_loc:
                                        curr = int(ws_stock.cell(c.row, 3).value)
                                        ws_stock.update_cell(c.row, 3, curr + qty)
                                        ws_stock.update_cell(c.row, 7, new_rp)
                                        ws_stock.update_cell(c.row, 8, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                                        break
                                log_transaction("REPLENISH", i_id, qty, src, t_loc)
                                st.success("Success"); st.rerun()
                            except Exception as e: st.error(e)
                else: st.warning("ไม่พบสินค้าใน Reserve")
        else: st.success("PICK Zone ปกติ")

# ==========================================
# 4. PICKING
# ==========================================
elif menu == "4. Picking (หยิบสินค้า)":
    st.header("🛒 4. Picking")
    # (Code V19)
    df = safe_get_data(ws_stock)
    if not df.empty:
        if 'pk_r' not in st.session_state: st.session_state.pk_r = 0
        il = df['Item_ID'].unique().tolist()
        dk = f"pks_{st.session_state.pk_r}"
        sp = st.selectbox("Item", il, index=None, key=dk)
        if sp:
            df['Item_ID'] = df['Item_ID'].astype(str)
            sl = df[df['Item_ID'] == str(sp)]
            if not sl.empty:
                st.dataframe(sl[['Location', 'Qty']])
                with st.form("pk"):
                    tl = st.selectbox("Loc", sl['Location'].unique())
                    # GUARD Picking: ห้ามหยิบเกิน
                    max_pick = int(sl[sl['Location'] == tl].iloc[0]['Qty'])
                    q = st.number_input("Qty", min_value=1, max_value=max_pick, value=1)
                    
                    if st.form_submit_button("Pick"):
                        cl = ws_stock.findall(str(sp))
                        for c in cl:
                            if ws_stock.cell(c.row, 4).value == tl:
                                cr = int(ws_stock.cell(c.row, 3).value)
                                if cr >= q:
                                    nq = cr - q
                                    if nq == 0: ws_stock.delete_rows(c.row)
                                    else: ws_stock.update_cell(c.row, 3, nq)
                                    log_transaction("PICKING", sp, q, tl, "OUT")
                                    st.toast("Picked"); st.session_state.pk_r += 1; st.rerun()
                                break
    else: st.info("No Data")

# ==========================================
# 5. SHIP OUT
# ==========================================
elif menu == "5. Ship Out (ขนส่ง)":
    st.header("🚚 5. Ship Out")

# ==========================================
# 6. ADD NEW ITEM (NEW FEATURE)
# ==========================================
elif menu == "6. Add New Item (เพิ่มสินค้าใหม่)":
    st.header("✨ 6. Add New Item & Photo")
    
    st.warning(f"📂 รูปจะถูกอัปโหลดไปที่ Drive Folder ID: {PICTURE_FOLDER_ID}")
    
    # 1. เชื่อมต่อ Drive
    drive_service = authenticate_drive()
    if not drive_service:
        st.error("❌ ไม่สามารถเชื่อมต่อ Google Drive ได้ (กรุณาเช็ค st.secrets['oauth'])")
    
    with st.container():
        st.subheader("📝 ข้อมูลสินค้า")
        
        # --- Input Form ---
        c1, c2 = st.columns([1, 2])
        with c1:
            # สแกนบาร์โค้ด
            new_barcode = st.text_input("Barcode สินค้า", key="new_item_barcode")
            cam_new = st.camera_input("สแกน Barcode (ถ้ามี)", key="cam_new_item")
            if cam_new:
                bc_val = decode_barcode_from_image(cam_new)
                if bc_val:
                    # Trick: update session state or show warning
                    st.info(f"Scanned: {bc_val}")
                    # ใน Streamlit ปกติการ set value กลับไป text_input ยาก 
                    # ให้ User พิมพ์ตาม หรือใช้ session_state logic ซับซ้อนกว่านี้
                    # เบื้องต้นแสดงค่าให้เห็น
        
        with c2:
            new_name = st.text_input("ชื่อสินค้า (Description)", key="new_item_name")
            new_category = st.text_input("หมวดหมู่ (Category)", key="new_item_cat")
            new_replen = st.number_input("จุดเติมของ (Replen Point)", min_value=1, value=10)

        st.divider()
        st.subheader("📸 รูปถ่ายสินค้า")
        
        # Camera Input สำหรับถ่ายรูปสินค้า
        product_photo = st.camera_input("ถ่ายรูปสินค้าเพื่อเก็บเข้าฐานข้อมูล", key="cam_product_photo")
        
        # --- Save Button ---
        if st.button("💾 บันทึกสินค้าใหม่", type="primary"):
            if not new_barcode or not new_name:
                st.error("กรุณาระบุ Barcode และ ชื่อสินค้า")
            elif not drive_service:
                st.error("Google Drive ไม่พร้อมใช้งาน")
            else:
                try:
                    image_id = "-"
                    image_link = "-"
                    
                    # 1. Upload Photo (ถ้ามีการถ่าย)
                    if product_photo:
                        with st.spinner("กำลังอัปโหลดรูปภาพ..."):
                            # ตั้งชื่อไฟล์เป็น Barcode_Timestamp.jpg
                            ts_file = datetime.now().strftime("%Y%m%d_%H%M%S")
                            filename = f"{new_barcode}_{ts_file}.jpg"
                            
                            image_id = upload_photo_to_drive(drive_service, product_photo, filename, PICTURE_FOLDER_ID)
                            image_link = f"https://drive.google.com/open?id={image_id}"
                            st.toast("✅ อัปโหลดรูปสำเร็จ")
                    
                    # 2. Save to Master Sheet
                    with st.spinner("กำลังบันทึกข้อมูล..."):
                        # Structure: [Barcode, Name, Category, Image_Link, Replen_Point, Timestamp]
                        # ปรับตาม Column ของ Item_Master จริงๆ ของคุณ
                        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        
                        # เช็คว่ามี Barcode ซ้ำไหม
                        existing = ws_item_master.findall(str(new_barcode))
                        if existing:
                            st.warning(f"⚠️ Barcode {new_barcode} มีอยู่แล้วในระบบ (แถว {existing[0].row}) - จะทำการเพิ่มต่อท้าย")
                        
                        # เพิ่มข้อมูล (Append)
                        # สมมติลำดับคอลัมน์: Barcode | Description | Category | Zone | Rack | Level | ... | Image | ...
                        # เพื่อความชัวร์ ผมจะต่อท้ายเป็น List ไป
                        new_row = [str(new_barcode), new_name, new_category, "", "", "", image_link, new_replen, timestamp]
                        
                        ws_item_master.append_row(new_row)
                        
                        st.success(f"บันทึกสินค้า **{new_name}** เรียบร้อย!")
                        if image_id != "-":
                            st.write(f"🔗 Link รูปภาพ: [Click Here]({image_link})")
                            
                except Exception as e:
                    st.error(f"เกิดข้อผิดพลาด: {e}")