import streamlit as st
import pandas as pd
import sqlite3
import time
import base64
import os
import hashlib
import pickle # Added to save the timetable for viewers
from ortools.sat.python import cp_model

# Set up the webpage configuration
st.set_page_config(page_title="School Timetable Pro", page_icon="🏫", layout="wide")

# --- TIMETABLE SAVING/LOADING ---
def save_timetable(schedule, classes, teachers, num_periods):
    """Saves the generated timetable to a file so viewers can see it."""
    with open("saved_timetable.pkl", "wb") as f:
        pickle.dump({
            "schedule": schedule,
            "classes": classes,
            "teachers": teachers,
            "num_periods": num_periods
        }, f)

def load_timetable():
    """Loads the saved timetable for teachers visiting the public link."""
    if os.path.exists("saved_timetable.pkl"):
        with open("saved_timetable.pkl", "rb") as f:
            data = pickle.load(f)
            st.session_state['schedule'] = data['schedule']
            st.session_state['classes'] = data['classes']
            st.session_state['teachers'] = data['teachers']
            st.session_state['num_periods'] = data['num_periods']

# Automatically load the timetable for anyone visiting the page
if 'schedule' not in st.session_state:
    load_timetable()

# --- DATABASE AUTO-MIGRATION ---
def auto_migrate_db():
    """Automatically upgrades the database schema to support new features like max_per_day, is_block, is_class_teacher, and is_last_period."""
    try:
        conn = sqlite3.connect('school_data.db')
        cursor = conn.cursor()
        
        # Check existing columns in Workloads
        cursor.execute("PRAGMA table_info(Workloads)")
        cols = [c[1] for c in cursor.fetchall()]
        
        if cols:
            if 'max_per_day' not in cols:
                cursor.execute("ALTER TABLE Workloads ADD COLUMN max_per_day INTEGER DEFAULT 1")
            if 'is_block' not in cols:
                cursor.execute("ALTER TABLE Workloads ADD COLUMN is_block INTEGER DEFAULT 0")
            if 'is_class_teacher' not in cols:
                cursor.execute("ALTER TABLE Workloads ADD COLUMN is_class_teacher INTEGER DEFAULT 0")
            if 'is_last_period' not in cols:
                cursor.execute("ALTER TABLE Workloads ADD COLUMN is_last_period INTEGER DEFAULT 0")
            conn.commit()
            
        conn.close()
    except Exception as e:
        pass

# Run migration immediately so the app doesn't crash on old databases
auto_migrate_db()


# --- TIMETABLE LOGIC ENGINE ---
def generate_timetable(num_periods=8):
    """Generates the optimal timetable based on database constraints."""
    # 1. Connect to Database and Fetch Data
    conn = sqlite3.connect('school_data.db')
    cursor = conn.cursor()

    # Fetch Teachers
    cursor.execute("SELECT id, name, designation FROM Teachers")
    teachers_data = cursor.fetchall()
    teacher_ids = [row[0] for row in teachers_data]
    teacher_names = {row[0]: row[1] for row in teachers_data}
    teacher_designations = {row[0]: row[2] for row in teachers_data}

    # Fetch Classes
    cursor.execute("SELECT id, grade_section FROM Classes")
    classes_data = cursor.fetchall()
    class_ids = [row[0] for row in classes_data]
    class_names = {row[0]: row[1] for row in classes_data}
    
    # Fetch Subjects
    cursor.execute("SELECT id, name FROM Subjects")
    subjects_data = cursor.fetchall()
    subject_names = {row[0]: row[1] for row in subjects_data}

    # Fetch Workloads (Now includes is_last_period!)
    cursor.execute("SELECT teacher_id, class_id, subject_id, periods_per_week, max_per_day, is_block, is_class_teacher, is_last_period FROM Workloads")
    workloads_data = cursor.fetchall()
    
    # Format workloads into a dictionary: (teacher, class, subject) -> {weekly, daily_max, is_block, is_class_teacher, is_last_period}
    weekly_requirements = {
        (row[0], row[1], row[2]): {
            'weekly': row[3], 
            'daily_max': row[4] if row[4] else 1,
            'is_block': bool(row[5] if len(row) > 5 and row[5] is not None else 0),
            'is_class_teacher': bool(row[6] if len(row) > 6 and row[6] is not None else 0),
            'is_last_period': bool(row[7] if len(row) > 7 and row[7] is not None else 0)
        } for row in workloads_data
    }
    conn.close()

    # 2. Setup OR-Tools Model
    model = cp_model.CpModel()
    num_days = 6

    # 3. Create Variables (The Grid)
    schedule = {}
    for (t, c, s) in weekly_requirements.keys():
        for d in range(num_days):
            for p in range(num_periods):
                name = f't{t}_c{c}_s{s}_d{d}_p{p}'
                schedule[(t, c, s, d, p)] = model.NewBoolVar(name)

    # 4. Hard Constraints
    # A teacher can only be in one class/subject at a time
    for t in teacher_ids:
        for d in range(num_days):
            for p in range(num_periods):
                model.AddAtMostOne(schedule[(tc, c, s, d, p)] for (tc, c, s) in weekly_requirements.keys() if tc == t)

    # A class can only have one teacher/subject per period
    for c in class_ids:
        for d in range(num_days):
            for p in range(num_periods):
                model.AddAtMostOne(schedule[(t, cc, s, d, p)] for (t, cc, s) in weekly_requirements.keys() if cc == c)

    # Enforce exact periods per week AND spread them across the days
    for (t, c, s), req in weekly_requirements.items():
        # 1. Total weekly target
        model.Add(sum(schedule[(t, c, s, d, p)] for d in range(num_days) for p in range(num_periods)) == req['weekly'])
        
        # 2. Daily limit per workload 
        # (Silently ensure daily_max is at least 2 if it's a block so they don't conflict)
        daily_max = req['daily_max']
        if req['is_block'] and daily_max < 2:
            daily_max = 2
            
        for d in range(num_days):
            model.Add(sum(schedule[(t, c, s, d, p)] for p in range(num_periods)) <= daily_max)

    # 5. Smart Constraints
    # Limit global daily periods dynamically to prevent overallocation
    for t in teacher_ids:
        if t not in teacher_designations:
            continue
            
        designation = teacher_designations[t]
        max_daily_overall = 6 if designation in ['PRT', 'Mother Teacher'] else 6 
        
        t_workloads = [(c, s) for (tc, c, s) in weekly_requirements.keys() if tc == t]
        if not t_workloads:
            continue
            
        for d in range(num_days):
            model.Add(sum(schedule[(t, c, s, d, p)] for (c, s) in t_workloads for p in range(num_periods)) <= max_daily_overall)

    # --- NEW: Class Teacher First Period Constraint ---
    for (t, c, s), req in weekly_requirements.items():
        if req['is_class_teacher']:
            # Force all assigned periods for this specific workload into Period 0 (The first period)
            for d in range(num_days):
                for p in range(1, num_periods): # Loops through all periods EXCEPT Period 0
                    model.Add(schedule[(t, c, s, d, p)] == 0)

    # --- NEW: Diary / Last Period Constraint ---
    for (t, c, s), req in weekly_requirements.items():
        if req['is_last_period']:
            # Force all assigned periods for this specific workload into the LAST period (num_periods - 1)
            for d in range(num_days):
                for p in range(num_periods - 1): # Loops through all periods EXCEPT the last one
                    model.Add(schedule[(t, c, s, d, p)] == 0)

    # --- NEW: Dynamic Double-Period Blocks (Labs, Art, etc.) ---
    for (t, c, s), req in weekly_requirements.items():
        if req['is_block']:
            # Create the valid patterns for a day: either 0 periods, or exactly 1 block of 2 contiguous periods.
            valid_lab_patterns = [tuple([0] * num_periods)]
            for start_p in range(num_periods - 1):
                pattern = [0] * num_periods
                pattern[start_p] = 1
                pattern[start_p + 1] = 1
                valid_lab_patterns.append(tuple(pattern))
                
            for d in range(num_days):
                daily_vars = [schedule[(t, c, s, d, p)] for p in range(num_periods)]
                model.AddAllowedAssignments(daily_vars, valid_lab_patterns)

    # --- NEW: Shared Room / Resource Capacity Constraints ---
    # The school has limited specialized rooms (1 of each).
    # Group subjects by keyword to ensure they don't overlap in the same room.
    specialized_room_keywords = ['music', 'computer', 'physics', 'chemistry', 'biology', 'art']
    
    for kw in specialized_room_keywords:
        # Find all subject IDs that contain this specific keyword
        matching_subjects = [s_id for s_id, s_name in subject_names.items() if kw in s_name.lower()]
        
        if matching_subjects:
            for d in range(num_days):
                for p in range(num_periods):
                    # Ensure only ONE class can be taking ANY subject related to this room per period
                    model.Add(
                        sum(schedule[(t, c, s, d, p)] 
                            for (t, c, s) in weekly_requirements.keys() if s in matching_subjects
                        ) <= 1
                    )

    # 6. Solve and Package
    solver = cp_model.CpSolver()
    status = solver.Solve(model)

    if status == cp_model.OPTIMAL or status == cp_model.FEASIBLE:
        master_schedule = {}
        for c in class_ids:
            master_schedule[c] = {}
            for d in range(num_days):
                master_schedule[c][d] = {}
                for p in range(num_periods):
                    assigned_value = "Free Period"
                    
                    for (t, cc, s) in weekly_requirements.keys():
                        if cc == c and solver.Value(schedule[(t, c, s, d, p)]) == 1:
                            assigned_value = f"{subject_names[s]} ({teacher_names[t]})"
                    
                    master_schedule[c][d][p] = assigned_value
                    
        return master_schedule, class_names, teacher_names
    else:
        return None, None, None


# --- CUSTOM UI: BACKGROUND, LOGO & COLORS ---
def set_background():
    """Injects custom CSS to add a background image. Supports local images via base64."""
    image_path = "building.jpg"
    
    if image_path.startswith("http"):
        bg_image_url = image_path
    elif os.path.exists(image_path):
        with open(image_path, "rb") as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode()
        mime_type = "image/png" if image_path.lower().endswith(".png") else "image/jpeg"
        bg_image_url = f"data:{mime_type};base64,{encoded_string}"
    else:
        bg_image_url = "https://images.unsplash.com/photo-1541829070764-84a7d30dd3f3?q=80&w=2069&auto=format&fit=crop"
    
    st.markdown(
        f"""
        <style>
        .stApp {{
            background-image: url("{bg_image_url}");
            background-attachment: fixed;
            background-size: cover;
            background-position: center;
        }}
        .block-container {{
            background-color: rgba(255, 255, 255, 0.90);
            padding: 2rem 3rem;
            border-radius: 15px;
            margin-top: 2rem;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        }}
        [data-testid="stSidebar"] {{
            background-color: rgba(240, 242, 246, 0.95);
        }}
        </style>
        """,
        unsafe_allow_html=True
    )

def color_schedule_cells(val):
    """Applies CSS styling to the dataframe cells based on their content."""
    if not isinstance(val, str):
        return ''
    
    if val == 'Free Period':
        return 'background-color: #d4edda; color: #155724;' # Soft Green
    elif val.startswith('Period '):
        return 'background-color: #e9ecef; color: #495057; font-weight: bold;' # Gray Header Column
    else:
        # Generate a completely unique, stable, and varied pastel color using an MD5 hash
        hash_int = int(hashlib.md5(val.encode('utf-8')).hexdigest(), 16)
        
        # Hue: 0-359 degrees (spreads across the entire color wheel)
        hue = hash_int % 360
        # Saturation: 65-85% (ensures the color is vivid but not overpowering)
        saturation = 65 + (hash_int % 20)
        # Lightness: 80-90% (keeps it light and pastel so black text is easy to read)
        lightness = 80 + ((hash_int // 360) % 10)
        
        color = f"hsl({hue}, {saturation}%, {lightness}%)"
        return f'background-color: {color}; color: #000000; font-weight: 500;'

set_background()

# --- DATABASE HELPER FUNCTIONS ---
def run_query(query, parameters=()):
    try:
        conn = sqlite3.connect('school_data.db')
        cursor = conn.cursor()
        cursor.execute(query, parameters)
        conn.commit()
        conn.close()
        return True, "Success"
    except Exception as e:
        return False, str(e)

def fetch_dropdown_data(table_name, display_cols):
    try:
        conn = sqlite3.connect('school_data.db')
        cursor = conn.cursor()
        
        if table_name == 'Teachers':
            cursor.execute("SELECT id, name, designation FROM Teachers")
            data = {row[0]: f"{row[1]} ({row[2]})" for row in cursor.fetchall()}
        elif table_name == 'Classes':
            cursor.execute("SELECT id, grade_section FROM Classes")
            data = {row[0]: row[1] for row in cursor.fetchall()}
        elif table_name == 'Subjects':
            cursor.execute("SELECT id, name FROM Subjects")
            data = {row[0]: row[1] for row in cursor.fetchall()}
        else:
            data = {}
            
        conn.close()
        return data
    except:
        return {}

def get_record(table, record_id):
    try:
        conn = sqlite3.connect('school_data.db')
        cursor = conn.cursor()
        cursor.execute(f"SELECT * FROM {table} WHERE id=?", (record_id,))
        res = cursor.fetchone()
        conn.close()
        return res
    except:
        return None

def fetch_workloads_dropdown():
    try:
        conn = sqlite3.connect('school_data.db')
        cursor = conn.cursor()
        cursor.execute('''
            SELECT w.id, t.name, c.grade_section, s.name, w.periods_per_week, w.max_per_day, w.is_block, w.is_class_teacher, w.is_last_period
            FROM Workloads w
            JOIN Teachers t ON w.teacher_id = t.id
            JOIN Classes c ON w.class_id = c.id
            JOIN Subjects s ON w.subject_id = s.id
        ''')
        data = {}
        for row in cursor.fetchall():
            block_txt = " [BLOCK]" if len(row) > 6 and row[6] else ""
            ct_txt = " [CLASS T.]" if len(row) > 7 and row[7] else ""
            lp_txt = " [LAST P.]" if len(row) > 8 and row[8] else ""
            data[row[0]] = f"{row[1]} ➔ {row[2]} ({row[4]} per/wk){block_txt}{ct_txt}{lp_txt}"
        conn.close()
        return data
    except:
        return {}

# --- SIDEBAR: DATA ENTRY & MANAGEMENT ---
st.sidebar.header("⚙️ Database Management")

# NEW: Hide the sidebar controls behind a password
admin_password = st.sidebar.text_input("Enter Admin Password to Edit:", type="password")

if admin_password == "admin123":
    st.sidebar.success("Admin Access Unlocked!")
    st.sidebar.markdown("Add new data, edit, or delete existing records.")

    tab_add, tab_mod, tab_del = st.sidebar.tabs(["➕ Add", "✏️ Edit", "🗑️ Delete"])

    # --- TAB 1: ADD NEW DATA ---
    with tab_add:
        with st.expander("👨‍🏫 Add New Teacher"):
            with st.form("add_teacher_form", clear_on_submit=True):
                t_name = st.text_input("Teacher Name")
                t_desig = st.text_input("Designation (e.g., Mother Teacher, PGT)") 
                t_spec = st.text_input("Specialization (e.g., Physics)")
                t_max = st.number_input("Max Periods Per Week", min_value=1, max_value=40, value=24)
                
                if st.form_submit_button("Save Teacher"):
                    if t_name and t_desig:
                        success, msg = run_query(
                            "INSERT INTO Teachers (name, designation, specialization, max_periods) VALUES (?, ?, ?, ?)",
                            (t_name, t_desig, t_spec, t_max)
                        )
                        if success: st.success(f"Added {t_name}!")
                        else: st.error("Database Error")
                    else:
                        st.warning("Name and Designation are required.")

        with st.expander("📚 Add New Class"):
            with st.form("add_class_form", clear_on_submit=True):
                c_grade = st.text_input("Grade & Section (e.g., Nursery-A)")
                c_stage = st.text_input("Stage (e.g., Foundational, Secondary)")
                
                if st.form_submit_button("Save Class"):
                    if c_grade:
                        success, msg = run_query(
                            "INSERT INTO Classes (grade_section, stage) VALUES (?, ?)",
                            (c_grade, c_stage)
                        )
                        if success: st.success(f"Added {c_grade}!")
                        else: st.error("Database Error")
                    else:
                        st.warning("Grade/Section is required.")

        with st.expander("📖 Add New Subject"):
            with st.form("add_subject_form", clear_on_submit=True):
                s_name = st.text_input("Subject Name")
                s_type = st.text_input("Type (e.g., Core, Lab)")
                
                if st.form_submit_button("Save Subject"):
                    if s_name:
                        success, msg = run_query(
                            "INSERT INTO Subjects (name, type) VALUES (?, ?)",
                            (s_name, s_type)
                        )
                        if success: st.success(f"Added {s_name}!")
                        else: st.error("Database Error")
                    else:
                        st.warning("Subject name is required.")

        with st.expander("⏱️ Assign Workload"):
            teacher_opts = fetch_dropdown_data('Teachers', [])
            class_opts = fetch_dropdown_data('Classes', [])
            subject_opts = fetch_dropdown_data('Subjects', [])
            
            with st.form("add_workload_form", clear_on_submit=True):
                if not teacher_opts or not class_opts or not subject_opts:
                    st.warning("Please add at least one Teacher, Class, and Subject first.")
                    st.form_submit_button("Save Workload", disabled=True)
                else:
                    w_teacher = st.selectbox("Select Teacher", options=list(teacher_opts.keys()), format_func=lambda x: teacher_opts[x])
                    w_class = st.selectbox("Select Class", options=list(class_opts.keys()), format_func=lambda x: class_opts[x])
                    w_subject = st.selectbox("Select Subject", options=list(subject_opts.keys()), format_func=lambda x: subject_opts[x])
                    
                    col1_w, col2_w = st.columns(2)
                    with col1_w:
                        w_periods = st.number_input("Periods/Week", min_value=1, max_value=40, value=6)
                    with col2_w:
                        w_max_per_day = st.number_input("Max/Day", min_value=1, max_value=8, value=1, help="If Block is checked, this acts as 2 automatically.")
                    
                    w_is_block = st.checkbox("Requires Double Period Block (e.g., Lab)?")
                    w_is_class_teacher = st.checkbox("Is Class Teacher (Force First Period)?", help="Locks this workload exclusively to Period 1 of the day.")
                    w_is_last_period = st.checkbox("Is Diary/Last Period (Force Last Period)?", help="Locks this workload exclusively to the last period of the day.")
                    
                    if w_is_block and (w_is_class_teacher or w_is_last_period):
                        st.warning("⚠️ Warning: A block teacher will take up two consecutive periods, not just one.")
                    if w_is_class_teacher and w_is_last_period:
                        st.warning("⚠️ Warning: You cannot simultaneously force a subject into the first and last period. Choose one.")

                    if st.form_submit_button("Save Workload"):
                        success, msg = run_query(
                            "INSERT INTO Workloads (teacher_id, class_id, subject_id, periods_per_week, max_per_day, is_block, is_class_teacher, is_last_period) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (w_teacher, w_class, w_subject, w_periods, w_max_per_day, int(w_is_block), int(w_is_class_teacher), int(w_is_last_period))
                        )
                        if success: st.success("Workload assigned!")
                        else: st.error("Database Error")

    # --- TAB 2: MODIFY EXISTING DATA ---
    with tab_mod:
        with st.expander("👨‍🏫 Edit Teacher"):
            t_opts_mod = fetch_dropdown_data('Teachers', [])
            if t_opts_mod:
                mod_t_id = st.selectbox("Select Teacher to Edit", options=list(t_opts_mod.keys()), format_func=lambda x: t_opts_mod[x], key="mod_t_sel")
                t_rec = get_record("Teachers", mod_t_id)
                if t_rec:
                    with st.form("mod_t_form"):
                        m_name = st.text_input("Name", value=t_rec[1])
                        m_desig = st.text_input("Designation", value=t_rec[2])
                        m_spec = st.text_input("Specialization", value=t_rec[3] if t_rec[3] else "")
                        m_max = st.number_input("Max Periods Per Week", min_value=1, max_value=40, value=t_rec[4])
                        
                        if st.form_submit_button("Update Teacher"):
                            s, m = run_query("UPDATE Teachers SET name=?, designation=?, specialization=?, max_periods=? WHERE id=?", (m_name, m_desig, m_spec, m_max, mod_t_id))
                            if s:
                                st.success("Updated! Refreshing...")
                                time.sleep(1)
                                st.rerun()
            else:
                st.info("No teachers available.")

        with st.expander("📚 Edit Class"):
            c_opts_mod = fetch_dropdown_data('Classes', [])
            if c_opts_mod:
                mod_c_id = st.selectbox("Select Class to Edit", options=list(c_opts_mod.keys()), format_func=lambda x: c_opts_mod[x], key="mod_c_sel")
                c_rec = get_record("Classes", mod_c_id)
                if c_rec:
                    with st.form("mod_c_form"):
                        m_grade = st.text_input("Grade & Section", value=c_rec[1])
                        m_stage = st.text_input("Stage", value=c_rec[2])
                        
                        if st.form_submit_button("Update Class"):
                            s, m = run_query("UPDATE Classes SET grade_section=?, stage=? WHERE id=?", (m_grade, m_stage, mod_c_id))
                            if s:
                                st.success("Updated! Refreshing...")
                                time.sleep(1)
                                st.rerun()
            else:
                st.info("No classes available.")

        with st.expander("📖 Edit Subject"):
            s_opts_mod = fetch_dropdown_data('Subjects', [])
            if s_opts_mod:
                mod_s_id = st.selectbox("Select Subject to Edit", options=list(s_opts_mod.keys()), format_func=lambda x: s_opts_mod[x], key="mod_s_sel")
                s_rec = get_record("Subjects", mod_s_id)
                if s_rec:
                    with st.form("mod_s_form"):
                        m_sname = st.text_input("Subject Name", value=s_rec[1])
                        m_stype = st.text_input("Type", value=s_rec[2])
                        
                        if st.form_submit_button("Update Subject"):
                            s, m = run_query("UPDATE Subjects SET name=?, type=? WHERE id=?", (m_sname, m_stype, mod_s_id))
                            if s:
                                st.success("Updated! Refreshing...")
                                time.sleep(1)
                                st.rerun()
            else:
                st.info("No subjects available.")

        with st.expander("⏱️ Edit Workload Periods"):
            w_opts_mod = fetch_workloads_dropdown()
            if w_opts_mod:
                mod_w_id = st.selectbox("Select Assigned Workload", options=list(w_opts_mod.keys()), format_func=lambda x: w_opts_mod[x], key="mod_w_sel")
                w_rec = get_record("Workloads", mod_w_id)
                if w_rec:
                    with st.form("mod_w_form"):
                        col1_em, col2_em = st.columns(2)
                        with col1_em:
                            m_periods = st.number_input("Update Periods Per Week", min_value=1, max_value=40, value=w_rec[4])
                        with col2_em:
                            existing_max_per_day = w_rec[5] if len(w_rec) > 5 and w_rec[5] is not None else 1
                            m_max_per_day = st.number_input("Update Max Per Day", min_value=1, max_value=8, value=existing_max_per_day)
                        
                        existing_is_block = bool(w_rec[6]) if len(w_rec) > 6 and w_rec[6] is not None else False
                        m_is_block = st.checkbox("Requires Double Period Block?", value=existing_is_block)

                        existing_is_class_teacher = bool(w_rec[7]) if len(w_rec) > 7 and w_rec[7] is not None else False
                        m_is_class_teacher = st.checkbox("Is Class Teacher (Force First Period)?", value=existing_is_class_teacher)
                        
                        existing_is_last_period = bool(w_rec[8]) if len(w_rec) > 8 and w_rec[8] is not None else False
                        m_is_last_period = st.checkbox("Is Diary/Last Period (Force Last Period)?", value=existing_is_last_period)
                        
                        if m_is_class_teacher and m_is_last_period:
                            st.warning("⚠️ Warning: You cannot simultaneously force a subject into the first and last period.")

                        if st.form_submit_button("Update Workload"):
                            s, m = run_query("UPDATE Workloads SET periods_per_week=?, max_per_day=?, is_block=?, is_class_teacher=?, is_last_period=? WHERE id=?", (m_periods, m_max_per_day, int(m_is_block), int(m_is_class_teacher), int(m_is_last_period), mod_w_id))
                            if s:
                                st.success("Updated! Refreshing...")
                                time.sleep(1)
                                st.rerun()
            else:
                st.info("No workloads assigned yet.")

    # --- TAB 3: DELETE DATA ---
    with tab_del:
        with st.expander("👨‍🏫 Delete Teacher"):
            t_opts_del = fetch_dropdown_data('Teachers', [])
            if t_opts_del:
                with st.form("del_t_form"):
                    del_t_id = st.selectbox("Select Teacher to Delete", options=list(t_opts_del.keys()), format_func=lambda x: t_opts_del[x], key="del_t_sel")
                    st.warning("⚠️ Deleting a teacher will also delete all workloads assigned to them.")
                    
                    if st.form_submit_button("Delete Teacher"):
                        run_query("DELETE FROM Workloads WHERE teacher_id=?", (del_t_id,))
                        s, m = run_query("DELETE FROM Teachers WHERE id=?", (del_t_id,))
                        if s:
                            st.success("Teacher deleted! Refreshing...")
                            time.sleep(1)
                            st.rerun()
            else:
                st.info("No teachers available.")

        with st.expander("📚 Delete Class"):
            c_opts_del = fetch_dropdown_data('Classes', [])
            if c_opts_del:
                with st.form("del_c_form"):
                    del_c_id = st.selectbox("Select Class to Delete", options=list(c_opts_del.keys()), format_func=lambda x: c_opts_del[x], key="del_c_sel")
                    st.warning("⚠️ Deleting a class will also delete all workloads assigned to it.")
                    
                    if st.form_submit_button("Delete Class"):
                        run_query("DELETE FROM Workloads WHERE class_id=?", (del_c_id,))
                        s, m = run_query("DELETE FROM Classes WHERE id=?", (del_c_id,))
                        if s:
                            st.success("Class deleted! Refreshing...")
                            time.sleep(1)
                            st.rerun()
            else:
                st.info("No classes available.")

        with st.expander("📖 Delete Subject"):
            s_opts_del = fetch_dropdown_data('Subjects', [])
            if s_opts_del:
                with st.form("del_s_form"):
                    del_s_id = st.selectbox("Select Subject to Delete", options=list(s_opts_del.keys()), format_func=lambda x: s_opts_del[x], key="del_s_sel")
                    st.warning("⚠️ Deleting a subject will also delete all workloads associated with it.")
                    
                    if st.form_submit_button("Delete Subject"):
                        run_query("DELETE FROM Workloads WHERE subject_id=?", (del_s_id,))
                        s, m = run_query("DELETE FROM Subjects WHERE id=?", (del_s_id,))
                        if s:
                            st.success("Subject deleted! Refreshing...")
                            time.sleep(1)
                            st.rerun()
            else:
                st.info("No subjects available.")

        with st.expander("⏱️ Delete Workload"):
            w_opts_del = fetch_workloads_dropdown()
            if w_opts_del:
                with st.form("del_w_form"):
                    del_w_id = st.selectbox("Select Workload to Delete", options=list(w_opts_del.keys()), format_func=lambda x: w_opts_del[x], key="del_w_sel")
                    
                    if st.form_submit_button("Delete Workload"):
                        s, m = run_query("DELETE FROM Workloads WHERE id=?", (del_w_id,))
                        if s:
                            st.success("Workload deleted! Refreshing...")
                            time.sleep(1)
                            st.rerun()
            else:
                st.info("No workloads available.")
else:
    # This shows to teachers when they open the link!
    st.sidebar.info("Dashboard is in View-Only mode. Please enter the password above to make changes.")


# --- MAIN DASHBOARD AREA ---
col1, col2 = st.columns([1, 10])

with col1:
    if os.path.exists("logo.png"):
        st.image("logo.png", width=80)
    elif os.path.exists("logo.jpg"):
        st.image("logo.jpg", width=80)
    else:
        fallback_url = "https://img.icons8.com/color/96/000000/school.png"
        st.image(fallback_url, width=80)

with col2:
    st.title("Maharshi Dattatreya School")

st.subheader("Automated Timetable Generation Dashboard")

# NEW: Hide the generator engine behind the admin password too
if admin_password == "admin123":
    st.markdown("Click the button below to run the constraint optimization engine and generate schedules based on your database.")
    st.divider()

    selected_num_periods = st.slider("Number of Periods per Day", min_value=4, max_value=10, value=8)

    # The Generate Button
    if st.button("🚀 Run Generator Engine", type="primary"):
        with st.spinner("Calculating optimal constraints..."):
            try:
                master_schedule, class_names, teacher_names = generate_timetable(selected_num_periods)
                
                if master_schedule:
                    st.success("✅ Timetable Generated Successfully!")
                    st.session_state['schedule'] = master_schedule
                    st.session_state['classes'] = class_names
                    st.session_state['teachers'] = teacher_names
                    st.session_state['num_periods'] = selected_num_periods
                    
                    # SAVE the schedule so teachers can view it later!
                    save_timetable(master_schedule, class_names, teacher_names, selected_num_periods)
                else:
                    st.error("❌ Failed to generate timetable. Constraints might be impossible. Check your Workloads in the database!")
                    
            except sqlite3.OperationalError as e:
                st.error(f"❌ **Database Error:** `{e}`")
                st.warning("⚠️ **How to fix this:** Run `python setup_db.py` in your terminal to create the database.")
            except Exception as e:
                st.error(f"❌ An unexpected error occurred: {e}")
else:
    st.markdown("👋 **Welcome to the Teacher Portal!** Select a tab below to view your schedule.")

st.divider()

if 'schedule' in st.session_state:
    st.markdown("### View Schedules")
    
    tab_class, tab_teacher = st.tabs(["📚 By Class", "👨‍🏫 By Teacher"])
    
    days_of_week = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
    
    # --- CLASS VIEW TAB ---
    with tab_class:
        class_options = st.session_state.get('classes', {})
        if class_options:
            selected_class_id = st.selectbox(
                "Select a Class:", 
                options=list(class_options.keys()), 
                format_func=lambda x: class_options[x]
            )
            
            schedule_data = st.session_state['schedule'][selected_class_id]
            table_data = []
            
            display_periods = st.session_state.get('num_periods', 8)
            for p in range(display_periods):
                row = {"Period": f"Period {p+1}"}
                for d in range(6):
                    row[days_of_week[d]] = schedule_data[d][p]
                table_data.append(row)
                
            df = pd.DataFrame(table_data)
            
            styled_df = df.style.map(color_schedule_cells) if hasattr(df.style, "map") else df.style.applymap(color_schedule_cells)
            st.dataframe(styled_df, width='stretch', hide_index=True)
            
    # --- TEACHER VIEW TAB ---
    with tab_teacher:
        teacher_options = st.session_state.get('teachers', {})
        if teacher_options:
            selected_teacher_id = st.selectbox(
                "Select a Teacher:", 
                options=list(teacher_options.keys()), 
                format_func=lambda x: teacher_options[x]
            )
            
            selected_teacher_name = teacher_options[selected_teacher_id]
            table_data = []
            
            display_periods = st.session_state.get('num_periods', 8)
            for p in range(display_periods):
                row = {"Period": f"Period {p+1}"}
                for d in range(6):
                    assigned_class = "Free Period"
                    
                    for class_id, class_schedule in st.session_state['schedule'].items():
                        cell_val = class_schedule[d][p]
                        
                        if cell_val != "Free Period" and f"({selected_teacher_name})" in cell_val:
                            subject_name = cell_val.split(" (")[0]
                            assigned_class = f"{st.session_state['classes'][class_id]} - {subject_name}"
                            break 
                            
                    row[days_of_week[d]] = assigned_class
                table_data.append(row)
                
            df = pd.DataFrame(table_data)
            
            styled_df = df.style.map(color_schedule_cells) if hasattr(df.style, "map") else df.style.applymap(color_schedule_cells)
            st.dataframe(styled_df, width='stretch', hide_index=True)
        else:
            st.info("Please click the 'Run Generator Engine' button above to load the latest teacher data!")