import streamlit as st
import pandas as pd
import sqlite3
import time
import base64
import os
import hashlib
import pickle
from ortools.sat.python import cp_model

# Set up the webpage configuration
st.set_page_config(page_title="School Timetable Pro", page_icon="🏫", layout="wide")

# --- TIMETABLE SAVING/LOADING ---
def save_timetable(schedule, classes, teachers, num_periods):
    with open("saved_timetable.pkl", "wb") as f:
        pickle.dump({
            "schedule": schedule,
            "classes": classes,
            "teachers": teachers,
            "num_periods": num_periods
        }, f)

def load_timetable():
    if os.path.exists("saved_timetable.pkl"):
        with open("saved_timetable.pkl", "rb") as f:
            data = pickle.load(f)
            st.session_state['schedule'] = data['schedule']
            st.session_state['classes'] = data['classes']
            st.session_state['teachers'] = data['teachers']
            st.session_state['num_periods'] = data['num_periods']

if 'schedule' not in st.session_state:
    load_timetable()

# --- DATABASE AUTO-MIGRATION ---
def auto_migrate_db():
    try:
        conn = sqlite3.connect('school_data.db')
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(Workloads)")
        cols = [c[1] for c in cursor.fetchall()]
        if cols:
            if 'max_per_day' not in cols: cursor.execute("ALTER TABLE Workloads ADD COLUMN max_per_day INTEGER DEFAULT 1")
            if 'is_block' not in cols: cursor.execute("ALTER TABLE Workloads ADD COLUMN is_block INTEGER DEFAULT 0")
            if 'is_class_teacher' not in cols: cursor.execute("ALTER TABLE Workloads ADD COLUMN is_class_teacher INTEGER DEFAULT 0")
            if 'is_last_period' not in cols: cursor.execute("ALTER TABLE Workloads ADD COLUMN is_last_period INTEGER DEFAULT 0")
            if 'combined_id' not in cols: cursor.execute("ALTER TABLE Workloads ADD COLUMN combined_id TEXT DEFAULT ''")
        cursor.execute("PRAGMA table_info(Teachers)")
        cols_t = [c[1] for c in cursor.fetchall()]
        if cols_t:
            if 'max_periods' not in cols_t: cursor.execute("ALTER TABLE Teachers ADD COLUMN max_periods INTEGER DEFAULT 40")
            if 'part_time_limit' not in cols_t: cursor.execute("ALTER TABLE Teachers ADD COLUMN part_time_limit INTEGER DEFAULT 0")
        conn.commit()
        conn.close()
    except Exception as e:
        pass

auto_migrate_db()

# --- TIMETABLE LOGIC ENGINE ---
def generate_timetable(num_periods=8, max_daily_limit=None, gapless_schedule=True):
    """Generates the optimal timetable based on database constraints."""
    conn = sqlite3.connect('school_data.db')
    cursor = conn.cursor()

    cursor.execute("SELECT id, name, designation, max_periods, part_time_limit FROM Teachers")
    teachers_data = cursor.fetchall()
    teacher_ids = [row[0] for row in teachers_data]
    teacher_names = {row[0]: row[1] for row in teachers_data}
    teacher_designations = {row[0]: row[2] for row in teachers_data}
    teacher_max_periods = {row[0]: row[3] if row[3] is not None else 60 for row in teachers_data}
    teacher_part_time = {row[0]: row[4] if len(row) > 4 and row[4] is not None else 0 for row in teachers_data}

    cursor.execute("SELECT id, grade_section FROM Classes")
    class_ids = [row[0] for row in cursor.fetchall()]
    class_names = {row[0]: row[1] for row in cursor.execute("SELECT id, grade_section FROM Classes").fetchall()}
    
    cursor.execute("SELECT id, name FROM Subjects")
    subjects_data = cursor.fetchall()
    subject_names = {row[0]: row[1] for row in subjects_data}

    # NEW: Fetch Workload ID (row[0]) to act as the ultimate unique identifier
    cursor.execute("SELECT id, teacher_id, class_id, subject_id, periods_per_week, max_per_day, is_block, is_class_teacher, is_last_period, combined_id FROM Workloads")
    workloads_data = cursor.fetchall()
    
    weekly_requirements = {
        (row[0], row[1], row[2], row[3]): { # Key is now: (Workload ID, Teacher, Class, Subject)
            'weekly': row[4], 
            'daily_max': row[5] if row[5] else 1,
            'is_block': bool(row[6] if len(row) > 6 and row[6] is not None else 0),
            'is_class_teacher': bool(row[7] if len(row) > 7 and row[7] is not None else 0),
            'is_last_period': bool(row[8] if len(row) > 8 and row[8] is not None else 0),
            'combined_id': str(row[9]).strip().lower().replace(" ", "") if len(row) > 9 and row[9] is not None else ""
        } for row in workloads_data
    }
    conn.close()

    model = cp_model.CpModel()
    num_days = 6

    # --- AUTO-SANITIZER ---
    class_first_periods = set()
    class_last_periods = set()
    teacher_first_periods = set()
    teacher_last_periods = set()
    
    for (w_id, t, c, s), req in weekly_requirements.items():
        if req['is_class_teacher'] and req['is_last_period']:
            req['is_last_period'] = False 
            
        # REMOVED the bad rule that was turning 'is_block' to False here!
            
        if req['is_block'] and req['weekly'] > num_days * 2:
            req['is_block'] = False

        if req['is_class_teacher']:
            if c in class_first_periods or t in teacher_first_periods:
                req['is_class_teacher'] = False
            else:
                class_first_periods.add(c)
                teacher_first_periods.add(t)
                
        if req['is_last_period']:
            if c in class_last_periods or t in teacher_last_periods:
                req['is_last_period'] = False
            else:
                class_last_periods.add(c)
                teacher_last_periods.add(t)
                
        # FIX: Allow up to 12 periods a week if it is a Double Block!
        if req['is_class_teacher'] or req['is_last_period']:
            max_allowed = num_days * 2 if req['is_block'] else num_days
            if req['weekly'] > max_allowed:
                req['weekly'] = max_allowed
                
        if req['is_block'] and req['weekly'] % 2 != 0:
            req['weekly'] = max(0, req['weekly'] - 1)
            
        min_required_daily = (req['weekly'] + num_days - 1) // num_days
        if req['daily_max'] < min_required_daily:
            req['daily_max'] = min_required_daily
        if req['is_block'] and req['daily_max'] < 2:
            req['daily_max'] = 2

    # --- EXPLICIT ERROR PRE-CHECKS ---
    class_assigned = {c_id: 0 for c_id in class_ids}
    class_groups_seen = {c_id: set() for c_id in class_ids}
    class_breakdown = {c_id: [] for c_id in class_ids}
    
    for (w_id, t, c, s), req in weekly_requirements.items():
        cid = req['combined_id']
        sub_name = subject_names.get(s, "Unknown Subject")
        if cid:
            if cid not in class_groups_seen[c]:
                class_groups_seen[c].add(cid)
                class_assigned[c] += req['weekly']
                class_breakdown[c].append(f"{sub_name}({req['weekly']})")
        else:
            class_assigned[c] += req['weekly']
            class_breakdown[c].append(f"{sub_name}({req['weekly']})")
            
    for c, total in class_assigned.items():
        if total > num_days * num_periods:
            breakdown_str = ", ".join(class_breakdown[c])
            return None, None, None, f"Class '{class_names.get(c, str(c))}' requires {total} periods, but grid has {num_days * num_periods} slots.\n\n**Breakdown:** {breakdown_str}"

    teacher_assigned = {t_id: 0 for t_id in teacher_ids}
    teacher_groups_seen = {t_id: set() for t_id in teacher_ids}
    teacher_breakdown = {t_id: [] for t_id in teacher_ids}
    
    for (w_id, t, c, s), req in weekly_requirements.items():
        cid = req['combined_id']
        c_name = class_names.get(c, "Unknown Class")
        if cid:
            if cid not in teacher_groups_seen[t]:
                teacher_groups_seen[t].add(cid)
                teacher_assigned[t] += req['weekly']
                teacher_breakdown[t].append(f"{c_name}({req['weekly']})")
        else:
            teacher_assigned[t] += req['weekly']
            teacher_breakdown[t].append(f"{c_name}({req['weekly']})")

    for t, total in teacher_assigned.items():
        max_possible = num_days * num_periods if max_daily_limit is None else num_days * max_daily_limit
        pt_limit = teacher_part_time[t]
        if pt_limit > 0:
            max_possible = min(max_possible, num_days * pt_limit)
        if total > max_possible:
            breakdown_str = ", ".join(teacher_breakdown[t])
            return None, None, None, f"Teacher '{teacher_names.get(t, str(t))}' has {total} periods, exceeding limit of {max_possible}.\n\n**Breakdown:** {breakdown_str}"

    for t in teacher_ids:
        if teacher_assigned[t] > teacher_max_periods[t]:
            teacher_max_periods[t] = teacher_assigned[t]

    shared_rooms_config = [
        (['music', 'library'], False), 
        (['art'], False),
        (['computer'], True), 
        (['physics'], True),
        (['chemistry'], True),
        (['biology'], True)
    ]
    
    for room_keywords, requires_lab in shared_rooms_config:
        matching_subjects = []
        for s_id, s_name in subject_names.items():
            s_name_lower = s_name.lower()
            if any(kw in s_name_lower for kw in room_keywords):
                if requires_lab and not any(l in s_name_lower for l in ['lab', 'practical']):
                    continue
                matching_subjects.append(s_id)
        if matching_subjects:
            room_total = 0
            room_groups_seen = set()
            for (w_id, t, c, s), req in weekly_requirements.items():
                if s in matching_subjects:
                    cid = req['combined_id']
                    if cid:
                        if cid not in room_groups_seen:
                            room_groups_seen.add(cid)
                            room_total += req['weekly']
                    else:
                        room_total += req['weekly']
                        
            if room_total > num_days * num_periods:
                return None, None, None, f"Shared room '{room_keywords[0].title()}' overloaded! {room_total} periods."

    # --- Create Variables ---
    schedule = {}
    for (w_id, t, c, s) in weekly_requirements.keys():
        for d in range(num_days):
            for p in range(num_periods):
                name = f'w{w_id}_t{t}_c{c}_s{s}_d{d}_p{p}'
                schedule[(w_id, t, c, s, d, p)] = model.NewBoolVar(name)

    # --- Combined Groups Equality Rule ---
    combined_groups = {}
    for (w_id, t, c, s), req in weekly_requirements.items():
        cid = req['combined_id']
        if cid:
            if cid not in combined_groups:
                combined_groups[cid] = []
            combined_groups[cid].append((w_id, t, c, s))
            
    for cid, group_workloads in combined_groups.items():
        base_w, base_t, base_c, base_s = group_workloads[0]
        for i in range(1, len(group_workloads)):
            other_w, other_t, other_c, other_s = group_workloads[i]
            for d in range(num_days):
                for p in range(num_periods):
                    model.Add(schedule[(base_w, base_t, base_c, base_s, d, p)] == schedule[(other_w, other_t, other_c, other_s, d, p)])

    # 4. Hard Constraints
    for t in teacher_ids:
        for d in range(num_days):
            for p in range(num_periods):
                period_vars = []
                seen_cids = set()
                for (w_id, tc, c, s), req in weekly_requirements.items():
                    if tc == t:
                        cid = req['combined_id']
                        if cid:
                            if cid not in seen_cids:
                                seen_cids.add(cid)
                                period_vars.append(schedule[(w_id, tc, c, s, d, p)])
                        else:
                            period_vars.append(schedule[(w_id, tc, c, s, d, p)])
                if period_vars:
                    model.AddAtMostOne(period_vars)

    for c in class_ids:
        for d in range(num_days):
            for p in range(num_periods):
                period_vars = []
                seen_cids = set()
                for (w_id, tc, cc, s), req in weekly_requirements.items():
                    if cc == c:
                        cid = req['combined_id']
                        if cid:
                            if cid not in seen_cids:
                                seen_cids.add(cid)
                                period_vars.append(schedule[(w_id, tc, cc, s, d, p)])
                        else:
                            period_vars.append(schedule[(w_id, tc, cc, s, d, p)])
                if period_vars:
                    model.AddAtMostOne(period_vars)

    for (w_id, t, c, s), req in weekly_requirements.items():
        # Total weekly periods
        model.Add(sum(schedule[(w_id, t, c, s, d, p)] for d in range(num_days) for p in range(num_periods)) == req['weekly'])
        
        # --- SUBJECT CONTINUITY (SMOOTHING) ---
        # If a non-lab subject has 6 or more periods, force at least 'N' periods EVERY day
        # (e.g., 9 periods / 6 days = at least 1 period every single day)
        min_daily = req['weekly'] // num_days
        enforce_min = (min_daily > 0) and not req['is_block']
        
        for d in range(num_days):
            daily_sum = sum(schedule[(w_id, t, c, s, d, p)] for p in range(num_periods))
            model.Add(daily_sum <= req['daily_max'])
            if enforce_min:
                model.Add(daily_sum >= min_daily)

    # --- Part-Time Teacher Limits ---
    for (w_id, t, c, s), req in weekly_requirements.items():
        pt_limit = teacher_part_time[t]
        if pt_limit > 0:
            for d in range(num_days):
                for p in range(pt_limit, num_periods):
                    model.Add(schedule[(w_id, t, c, s, d, p)] == 0)

    # --- GAPLESS SCHEDULE FOR CLASSES ---
    if gapless_schedule:
        for c in class_ids:
            for d in range(num_days):
                for p in range(num_periods - 1):
                    active_p = []
                    active_next = []
                    seen_cids = set()
                    
                    for (w_id, tc, cc, s), req in weekly_requirements.items():
                        if cc == c and not req['is_last_period']:
                            cid = req['combined_id']
                            if cid:
                                if cid not in seen_cids:
                                    seen_cids.add(cid)
                                    active_p.append(schedule[(w_id, tc, cc, s, d, p)])
                                    active_next.append(schedule[(w_id, tc, cc, s, d, p + 1)])
                            else:
                                active_p.append(schedule[(w_id, tc, cc, s, d, p)])
                                active_next.append(schedule[(w_id, tc, cc, s, d, p + 1)])
                                
                    if active_p and active_next:
                        model.Add(sum(active_p) >= sum(active_next))

    # 5. Smart Constraints
    for t in teacher_ids:
        total_period_vars = []
        seen_cids_total = set()
        for (w_id, tc, c, s), req in weekly_requirements.items():
            if tc == t:
                cid = req['combined_id']
                if cid:
                    if cid not in seen_cids_total:
                        seen_cids_total.add(cid)
                        total_period_vars.extend([schedule[(w_id, tc, c, s, d, p)] for d in range(num_days) for p in range(num_periods)])
                else:
                    total_period_vars.extend([schedule[(w_id, tc, c, s, d, p)] for d in range(num_days) for p in range(num_periods)])
                    
        if total_period_vars:
            model.Add(sum(total_period_vars) <= teacher_max_periods[t])
            
        for d in range(num_days):
            limit = max_daily_limit if max_daily_limit is not None else num_periods
            period_vars = []
            seen_cids = set()
            for (w_id, tc, c, s), req in weekly_requirements.items():
                if tc == t:
                    cid = req['combined_id']
                    if cid:
                        if cid not in seen_cids:
                            seen_cids.add(cid)
                            period_vars.extend([schedule[(w_id, tc, c, s, d, p)] for p in range(num_periods)])
                    else:
                        period_vars.extend([schedule[(w_id, tc, c, s, d, p)] for p in range(num_periods)])
            if period_vars:
                model.Add(sum(period_vars) <= limit)

    # --- TEACHER-CLASS FATIGUE CONSTRAINT ---
    # Prevents a teacher from teaching the same class for 3+ periods in a day (e.g. Core + Double Lab)
    for t in teacher_ids:
        for c in class_ids:
            total_weekly_for_tc = 0
            seen_cids_tc = set()
            for (w_id, tc, cc, s), req in weekly_requirements.items():
                if tc == t and cc == c:
                    cid = req['combined_id']
                    if cid:
                        if cid not in seen_cids_tc:
                            seen_cids_tc.add(cid)
                            total_weekly_for_tc += req['weekly']
                    else:
                        total_weekly_for_tc += req['weekly']
            
            if total_weekly_for_tc > 0:
                # Normal subjects are capped at 2 per day. Mother teachers scale up automatically.
                safe_daily_max = max(2, (total_weekly_for_tc + num_days - 1) // num_days)
                for d in range(num_days):
                    daily_tc_vars = []
                    seen_cids_d = set()
                    for (w_id, tc, cc, s), req in weekly_requirements.items():
                        if tc == t and cc == c:
                            cid = req['combined_id']
                            if cid:
                                if cid not in seen_cids_d:
                                    seen_cids_d.add(cid)
                                    daily_tc_vars.extend([schedule[(w_id, tc, cc, s, d, p)] for p in range(num_periods)])
                            else:
                                daily_tc_vars.extend([schedule[(w_id, tc, cc, s, d, p)] for p in range(num_periods)])
                    
                    if daily_tc_vars:
                        model.Add(sum(daily_tc_vars) <= safe_daily_max)


    for (w_id, t, c, s), req in weekly_requirements.items():
        if req['is_class_teacher']:
            for d in range(num_days):
                if req['is_block']:
                    for p in range(2, num_periods): model.Add(schedule[(w_id, t, c, s, d, p)] == 0)
                else:
                    for p in range(1, num_periods): model.Add(schedule[(w_id, t, c, s, d, p)] == 0)

    for (w_id, t, c, s), req in weekly_requirements.items():
        if req['is_last_period']:
            for d in range(num_days):
                if req['is_block']:
                    for p in range(num_periods - 2): model.Add(schedule[(w_id, t, c, s, d, p)] == 0)
                else:
                    for p in range(num_periods - 1): model.Add(schedule[(w_id, t, c, s, d, p)] == 0)

    for (w_id, t, c, s), req in weekly_requirements.items():
        if req['is_block']:
            valid_lab_patterns = [tuple([0] * num_periods)]
            for start_p in range(num_periods - 1):
                pattern = [0] * num_periods
                pattern[start_p] = 1
                pattern[start_p + 1] = 1
                valid_lab_patterns.append(tuple(pattern))
                
            for d in range(num_days):
                daily_vars = [schedule[(w_id, t, c, s, d, p)] for p in range(num_periods)]
                model.AddAllowedAssignments(daily_vars, valid_lab_patterns)

    # --- Shared Room / Resource Capacity Constraints ---
    for room_keywords, requires_lab in shared_rooms_config:
        matching_subjects = []
        for s_id, s_name in subject_names.items():
            s_name_lower = s_name.lower()
            if any(kw in s_name_lower for kw in room_keywords):
                if requires_lab and not any(l in s_name_lower for l in ['lab', 'practical']):
                    continue
                matching_subjects.append(s_id)
                
        if matching_subjects:
            for d in range(num_days):
                for p in range(num_periods):
                    model.Add(sum(schedule[(w_id, t, c, s, d, p)] for (w_id, t, c, s) in weekly_requirements.keys() if s in matching_subjects) <= 1)

    # --- Base Subject vs Grammar Separation Constraint ---
    grammar_subjects = {s: name.lower().replace('grammar', '').strip() for s, name in subject_names.items() if 'grammar' in name.lower()}
    
    for gram_s, base_name in grammar_subjects.items():
        base_subjects = [s for s, name in subject_names.items() if base_name in name.lower() and 'grammar' not in name.lower()]
        if base_subjects:
            for c in class_ids:
                total_req = 0
                gram_groups_seen = set()
                for (w_id, t, cc, s), req in weekly_requirements.items():
                    if cc == c and (s == gram_s or s in base_subjects):
                        cid = req['combined_id']
                        if cid:
                            if cid not in gram_groups_seen:
                                gram_groups_seen.add(cid)
                                total_req += req['weekly']
                        else:
                            total_req += req['weekly']
                
                if total_req <= num_days:
                    for d in range(num_days):
                        gram_vars = [schedule[(w_id, t, c, gram_s, d, p)] for (w_id, t, cc, s) in weekly_requirements.keys() if cc == c and s == gram_s for p in range(num_periods)]
                        base_vars = [schedule[(w_id, t, c, s, d, p)] for (w_id, t, cc, s) in weekly_requirements.keys() if cc == c and s in base_subjects for p in range(num_periods)]
                        
                        if gram_vars and base_vars:
                            gram_active = model.NewBoolVar(f'gram_act_{gram_s}_c{c}_d{d}')
                            base_active = model.NewBoolVar(f'base_act_{gram_s}_c{c}_d{d}')
                            model.AddMaxEquality(gram_active, gram_vars)
                            model.AddMaxEquality(base_active, base_vars)
                            model.Add(gram_active + base_active <= 1)

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
                    assigned_values = []
                    # No longer checking seen_cids here so both electives will print properly!
                    for (w_id, t, cc, s) in weekly_requirements.keys():
                        if cc == c and solver.Value(schedule[(w_id, t, c, s, d, p)]) == 1:
                            assigned_values.append(f"{subject_names[s]} ({teacher_names[t]})")
                    
                    if assigned_values:
                        master_schedule[c][d][p] = " / ".join(assigned_values)
                    else:
                        master_schedule[c][d][p] = "Free Period"
        return master_schedule, class_names, teacher_names, None
    else:
        return None, None, None, None

# --- AUTO-REGENERATE WRAPPER ---
def handle_db_change(success, error_msg):
    """Triggers a fast UI refresh when DB changes are made without auto-running the heavy engine."""
    if success:
        st.success("✅ Database updated! (Click 'Run Generator Engine Manually' below when you are finished making changes).")
        time.sleep(1)
        st.rerun()
    else:
        st.error(f"Database Error: {error_msg}")


# --- CUSTOM UI: STYLES ---
def set_background():
    image_path = "building.jpg"
    if image_path.startswith("http"): bg_image_url = image_path
    elif os.path.exists(image_path):
        with open(image_path, "rb") as image_file:
            bg_image_url = f"data:image/jpeg;base64,{base64.b64encode(image_file.read()).decode()}"
    else:
        bg_image_url = "https://images.unsplash.com/photo-1541829070764-84a7d30dd3f3?q=80&w=2069&auto=format&fit=crop"
    
    st.markdown(
        f"""
        <style>
        .stApp {{ background-image: url("{bg_image_url}"); background-attachment: fixed; background-size: cover; background-position: center; }}
        .block-container {{ background-color: rgba(255, 255, 255, 0.90); padding: 2rem 3rem; border-radius: 15px; margin-top: 2rem; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }}
        [data-testid="stSidebar"] {{ background-color: rgba(240, 242, 246, 0.95); }}
        </style>
        """, unsafe_allow_html=True)

def color_class_cells(val, search_query=""):
    if not isinstance(val, str): return ''
    classes_list = list(st.session_state.get('classes', {}).values()) if 'classes' in st.session_state else []
    
    if val.startswith('Period ') or val in classes_list: return 'background-color: #e9ecef; color: #495057; font-weight: bold;'
    if val == 'Free Period': return 'background-color: #f1f3f5; color: #adb5bd;'
    if search_query and search_query.lower() in val.lower(): return 'background-color: #fff3cd; color: #856404; border: 2px solid #ffc107; font-weight: bold;'
    elif search_query: return 'background-color: #f8f9fa; color: #ced4da;'
        
    hash_int = int(hashlib.md5(val.encode('utf-8')).hexdigest(), 16)
    hue = hash_int % 360
    return f'background-color: hsl({hue}, {65 + (hash_int % 20)}%, {80 + ((hash_int // 360) % 10)}%); color: #000000; font-weight: 500;'

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

def fetch_dropdown_data(table_name):
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
        else: data = {}
        conn.close()
        return data
    except: return {}

def fetch_workloads_dropdown():
    try:
        conn = sqlite3.connect('school_data.db')
        cursor = conn.cursor()
        cursor.execute('''
            SELECT w.id, t.name, c.grade_section, s.name, w.periods_per_week, w.is_block, w.is_class_teacher, w.is_last_period, w.combined_id
            FROM Workloads w JOIN Teachers t ON w.teacher_id = t.id JOIN Classes c ON w.class_id = c.id JOIN Subjects s ON w.subject_id = s.id
        ''')
        data = {}
        for row in cursor.fetchall():
            extras = []
            if len(row)>5 and row[5]: extras.append("BLOCK")
            if len(row)>6 and row[6]: extras.append("CLASS T.")
            if len(row)>7 and row[7]: extras.append("LAST P.")
            if len(row)>8 and row[8]: extras.append(f"Linked:{row[8]}")
            extra_txt = f" [{', '.join(extras)}]" if extras else ""
            data[row[0]] = f"{row[1]} ➔ {row[2]} | {row[3]} ({row[4]} per/wk){extra_txt}"
        conn.close()
        return data
    except: return {}

def get_room_occupancy_stats():
    """Calculates the total used periods for all shared rooms."""
    try:
        conn = sqlite3.connect('school_data.db')
        cursor = conn.cursor()
        cursor.execute("SELECT id, name FROM Subjects")
        subject_names = {row[0]: row[1] for row in cursor.fetchall()}
        
        cursor.execute("SELECT subject_id, periods_per_week, combined_id FROM Workloads")
        workloads = cursor.fetchall()
        conn.close()

        shared_rooms_config = [
            (['music', 'library'], False), 
            (['art'], False),
            (['computer'], True), 
            (['physics'], True),
            (['chemistry'], True),
            (['biology'], True)
        ]
        
        stats = []
        for room_keywords, requires_lab in shared_rooms_config:
            matching_subjects = []
            for s_id, s_name in subject_names.items():
                s_name_lower = s_name.lower()
                if any(kw in s_name_lower for kw in room_keywords):
                    if requires_lab and not any(l in s_name_lower for l in ['lab', 'practical']):
                        continue
                    matching_subjects.append(s_id)
            
            if not matching_subjects:
                continue
                
            room_total = 0
            room_groups_seen = set()
            
            for s_id, periods, combined_id in workloads:
                if s_id in matching_subjects:
                    cid = str(combined_id).strip().lower().replace(" ", "") if combined_id else ""
                    if cid:
                        if cid not in room_groups_seen:
                            room_groups_seen.add(cid)
                            room_total += periods
                    else:
                        room_total += periods
                        
            room_name = "Music & Library Room" if 'music' in room_keywords else room_keywords[0].title() + (" Lab" if requires_lab else " Room")
            stats.append((room_name, room_total))
        return stats
    except Exception as e:
        return []

# --- SIDEBAR: DATA ENTRY & MANAGEMENT ---
st.sidebar.header("⚙️ Database Management")
admin_password = st.sidebar.text_input("Enter Admin Password to Edit:", type="password")

if admin_password == "admin123":
    st.sidebar.success("Admin Access Unlocked!")
    tab_add, tab_mod, tab_del = st.sidebar.tabs(["➕ Add", "✏️ Edit", "🗑️ Delete"])

    # --- ADD DATA ---
    with tab_add:
        with st.expander("📊 Shared Room Occupancy", expanded=False):
            st.caption("Check real-time capacity before assigning new Lab/Library periods.")
            room_stats = get_room_occupancy_stats()
            # Calculate max capacity (6 days * selected number of periods)
            max_cap = 6 * st.session_state.get('num_periods', 8)
            
            html_str = ""
            over_capacity = False
            for r_name, r_total in room_stats:
                pct = min((r_total / max_cap) * 100, 100) if max_cap > 0 else 0
                color = "#28a745" # Safe Green
                if pct > 95: color = "#dc3545" # Overloaded Red
                elif pct > 75: color = "#ffc107" # Warning Yellow
                if r_total > max_cap: over_capacity = True
                
                html_str += f"""
                <div style="margin-bottom: 12px;">
                    <div style="display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 4px; color: #495057;">
                        <strong>{r_name}</strong>
                        <span>{r_total} / {max_cap}</span>
                    </div>
                    <div style="width: 100%; background-color: #e9ecef; border-radius: 4px; height: 8px;">
                        <div style="width: {pct}%; background-color: {color}; height: 8px; border-radius: 4px;"></div>
                    </div>
                </div>
                """
            st.markdown(html_str, unsafe_allow_html=True)
            if over_capacity:
                st.error("⚠️ One or more shared rooms are currently over capacity!")

        with st.expander("👨‍🏫 Add New Teacher"):
            with st.form("add_teacher_form"):
                t_name = st.text_input("Teacher Name")
                t_desig = st.text_input("Designation (e.g., Mother Teacher, PGT)") 
                t_spec = st.text_input("Specialization (e.g., Physics)")
                t_max = st.number_input("Max Periods Per Week", min_value=1, max_value=60, value=40)
                t_part = st.number_input("Avail. Only First 'N' Periods/Day (0 = Full Day)", min_value=0, max_value=10, value=0, help="For guest teachers. Enter 4 to block them from period 5 onwards.")
                
                if st.form_submit_button("Save Teacher"):
                    if t_name and t_desig:
                        s, m = run_query("INSERT INTO Teachers (name, designation, specialization, max_periods, part_time_limit) VALUES (?, ?, ?, ?, ?)", (t_name, t_desig, t_spec, t_max, t_part))
                        handle_db_change(s, m)
                    else: st.warning("Name and Designation required.")

        with st.expander("📚 Add New Class"):
            with st.form("add_class_form"):
                c_grade = st.text_input("Grade & Section (e.g., Nursery-A)")
                c_stage = st.text_input("Stage (e.g., Foundational, Secondary)")
                if st.form_submit_button("Save Class"):
                    if c_grade:
                        s, m = run_query("INSERT INTO Classes (grade_section, stage) VALUES (?, ?)", (c_grade, c_stage))
                        handle_db_change(s, m)
                    else: st.warning("Grade/Section required.")

        with st.expander("📖 Add New Subject"):
            with st.form("add_subject_form"):
                s_name = st.text_input("Subject Name")
                s_type = st.text_input("Type (e.g., Core, Lab)")
                if st.form_submit_button("Save Subject"):
                    if s_name:
                        s, m = run_query("INSERT INTO Subjects (name, type) VALUES (?, ?)", (s_name, s_type))
                        handle_db_change(s, m)
                    else: st.warning("Subject name required.")

        with st.expander("⏱️ Assign Workload"):
            teacher_opts = fetch_dropdown_data('Teachers')
            class_opts = fetch_dropdown_data('Classes')
            subject_opts = fetch_dropdown_data('Subjects')
            
            with st.form("add_workload_form"):
                if not teacher_opts or not class_opts or not subject_opts:
                    st.warning("Add Teacher, Class, and Subject first.")
                    st.form_submit_button("Save Workload", disabled=True)
                else:
                    w_teacher = st.selectbox("Select Teacher", options=list(teacher_opts.keys()), format_func=lambda x: teacher_opts[x])
                    w_class = st.selectbox("Select Class", options=list(class_opts.keys()), format_func=lambda x: class_opts[x])
                    w_subject = st.selectbox("Select Subject", options=list(subject_opts.keys()), format_func=lambda x: subject_opts[x])
                    
                    c1, c2 = st.columns(2)
                    w_periods = c1.number_input("Periods/Week", min_value=1, max_value=60, value=6)
                    w_max_per_day = c2.number_input("Max/Day", min_value=1, max_value=8, value=1)
                    
                    w_cid = st.text_input("Combined Group ID (Optional)", help="Type a unique code (e.g. 'Hindi11') to merge multiple classes together for this subject, OR to allow simultaneous Electives in the same class.")
                    w_is_block = st.checkbox("Requires Double Period Block (e.g., Lab)?")
                    w_is_class_teacher = st.checkbox("Is Class Teacher (Force First Period)?")
                    w_is_last_period = st.checkbox("Is Diary/Last Period (Force Last Period)?")
                    
                    if st.form_submit_button("Save Workload"):
                        s, m = run_query(
                            "INSERT INTO Workloads (teacher_id, class_id, subject_id, periods_per_week, max_per_day, is_block, is_class_teacher, is_last_period, combined_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (w_teacher, w_class, w_subject, w_periods, w_max_per_day, int(w_is_block), int(w_is_class_teacher), int(w_is_last_period), w_cid)
                        )
                        handle_db_change(s, m)

    # --- EDIT DATA ---
    with tab_mod:
        with st.expander("👨‍🏫 Edit Teacher"):
            t_opts = fetch_dropdown_data('Teachers')
            if t_opts:
                mod_t_id = st.selectbox("Select Teacher", options=list(t_opts.keys()), format_func=lambda x: t_opts[x], key="mod_t")
                conn = sqlite3.connect('school_data.db')
                t_rec = conn.execute("SELECT * FROM Teachers WHERE id=?", (mod_t_id,)).fetchone()
                conn.close()
                if t_rec:
                    with st.form("mod_t_form"):
                        m_name = st.text_input("Name", value=t_rec[1])
                        m_desig = st.text_input("Designation", value=t_rec[2])
                        m_spec = st.text_input("Specialization", value=t_rec[3] if t_rec[3] else "")
                        m_max = st.number_input("Max Periods Per Week", min_value=1, max_value=60, value=int(t_rec[4]) if len(t_rec)>4 and t_rec[4] else 40)
                        m_part = st.number_input("Avail. Only First 'N' Periods/Day", min_value=0, max_value=10, value=int(t_rec[5]) if len(t_rec)>5 and t_rec[5] else 0)
                        
                        if st.form_submit_button("Update Teacher"):
                            s, m = run_query("UPDATE Teachers SET name=?, designation=?, specialization=?, max_periods=?, part_time_limit=? WHERE id=?", (m_name, m_desig, m_spec, m_max, m_part, mod_t_id))
                            handle_db_change(s, m)

        with st.expander("⏱️ Edit Workload"):
            w_opts = fetch_workloads_dropdown()
            if w_opts:
                mod_w_id = st.selectbox("Select Workload", options=list(w_opts.keys()), format_func=lambda x: w_opts[x], key="mod_w")
                conn = sqlite3.connect('school_data.db')
                w_rec = conn.execute("SELECT * FROM Workloads WHERE id=?", (mod_w_id,)).fetchone()
                conn.close()
                
                t_opts = fetch_dropdown_data('Teachers')
                c_opts = fetch_dropdown_data('Classes')
                s_opts = fetch_dropdown_data('Subjects')
                
                if w_rec and t_opts and c_opts and s_opts:
                    with st.form("mod_w_form"):
                        m_w_t = st.selectbox("Teacher", options=list(t_opts.keys()), format_func=lambda x: t_opts[x], index=list(t_opts.keys()).index(w_rec[1]) if w_rec[1] in t_opts else 0)
                        m_w_c = st.selectbox("Class", options=list(c_opts.keys()), format_func=lambda x: c_opts[x], index=list(c_opts.keys()).index(w_rec[2]) if w_rec[2] in c_opts else 0)
                        m_w_s = st.selectbox("Subject", options=list(s_opts.keys()), format_func=lambda x: s_opts[x], index=list(s_opts.keys()).index(w_rec[3]) if w_rec[3] in s_opts else 0)
                        
                        c1, c2 = st.columns(2)
                        m_per = c1.number_input("Periods/Week", min_value=1, max_value=60, value=w_rec[4])
                        m_mpd = c2.number_input("Max/Day", min_value=1, max_value=8, value=w_rec[5] if len(w_rec)>5 and w_rec[5] else 1)
                        
                        m_cid = st.text_input("Combined Group ID", value=str(w_rec[9]) if len(w_rec)>9 and w_rec[9] else "")
                        m_ib = st.checkbox("Requires Double Period Block?", value=bool(w_rec[6]) if len(w_rec)>6 and w_rec[6] else False)
                        m_ict = st.checkbox("Is Class Teacher (Force First)?", value=bool(w_rec[7]) if len(w_rec)>7 and w_rec[7] else False)
                        m_ilp = st.checkbox("Is Diary/Last Period (Force Last)?", value=bool(w_rec[8]) if len(w_rec)>8 and w_rec[8] else False)

                        if st.form_submit_button("Update Workload"):
                            s, m = run_query("UPDATE Workloads SET teacher_id=?, class_id=?, subject_id=?, periods_per_week=?, max_per_day=?, is_block=?, is_class_teacher=?, is_last_period=?, combined_id=? WHERE id=?", 
                                             (m_w_t, m_w_c, m_w_s, m_per, m_mpd, int(m_ib), int(m_ict), int(m_ilp), m_cid, mod_w_id))
                            handle_db_change(s, m)

    # --- DELETE DATA ---
    with tab_del:
        with st.expander("🗑️ Delete Data"):
            del_type = st.radio("What to delete?", ["Workload", "Teacher", "Class", "Subject"])
            opts = fetch_workloads_dropdown() if del_type == "Workload" else fetch_dropdown_data(del_type + 's')
            
            if opts:
                with st.form("del_form"):
                    del_id = st.selectbox(f"Select {del_type} to Delete", options=list(opts.keys()), format_func=lambda x: opts[x])
                    if del_type != "Workload": st.warning(f"⚠️ Deleting this will also delete all associated workloads.")
                    
                    if st.form_submit_button(f"Delete {del_type}"):
                        if del_type == "Teacher": run_query("DELETE FROM Workloads WHERE teacher_id=?", (del_id,))
                        elif del_type == "Class": run_query("DELETE FROM Workloads WHERE class_id=?", (del_id,))
                        elif del_type == "Subject": run_query("DELETE FROM Workloads WHERE subject_id=?", (del_id,))
                        
                        table_map = {"Workload":"Workloads", "Teacher":"Teachers", "Class":"Classes", "Subject":"Subjects"}
                        s, m = run_query(f"DELETE FROM {table_map[del_type]} WHERE id=?", (del_id,))
                        handle_db_change(s, m)
else:
    st.sidebar.info("Dashboard is in View-Only mode. Please enter the password above to make changes.")

# --- MAIN DASHBOARD AREA ---
col1, col2 = st.columns([1, 10])
with col1:
    if os.path.exists("logo.png"): st.image("logo.png", width=80)
    elif os.path.exists("logo.jpg"): st.image("logo.jpg", width=80)
    else: st.image("https://img.icons8.com/color/96/000000/school.png", width=80)
with col2:
    st.title("Maharshi Dattatreya School")

st.subheader("Automated Timetable Generation Dashboard")
st.markdown("💡 **Tip:** Once generated, click the **Download (CSV/Excel)** button under any tab. The CSV opens perfectly in Microsoft Excel, where you can easily print it or 'Save as PDF'!")

if admin_password == "admin123":
    st.divider()
    selected_num_periods = st.slider("Number of Periods per Day", min_value=4, max_value=10, value=8)
    
    col_t1, col_t2 = st.columns(2)
    with col_t1:
        st.info("🛡️ **Teacher Well-being:** By default, teachers are guaranteed at least one free period per day.")
        allow_full_day = st.checkbox("⚠️ Allow booking a teacher for ALL periods in a day", value=False)
        max_daily_limit = selected_num_periods if allow_full_day else selected_num_periods - 1
    with col_t2:
        st.info("🎓 **Student Well-being:** Free periods are grouped at the end of the day so students can leave early.")
        gapless_schedule = st.checkbox("📦 Force a continuous schedule for all classes (No free gaps)", value=True)

    if st.button("🚀 Run Generator Engine Manually", type="primary"):
        # Save explicit settings for auto-generator to use later
        st.session_state['num_periods'] = selected_num_periods
        st.session_state['max_daily_limit'] = max_daily_limit
        st.session_state['gapless_schedule'] = gapless_schedule
        
        with st.spinner("Calculating optimal constraints..."):
            try:
                master_schedule, class_names, teacher_names, error_msg = generate_timetable(selected_num_periods, max_daily_limit, gapless_schedule)
                if master_schedule:
                    st.success("✅ Timetable Generated Successfully!")
                    st.session_state['schedule'] = master_schedule
                    st.session_state['classes'] = class_names
                    st.session_state['teachers'] = teacher_names
                    save_timetable(master_schedule, class_names, teacher_names, selected_num_periods)
                elif error_msg:
                    st.error(f"❌ **Impossible Data Detected:** {error_msg}")
                    st.warning("Please modify the workload from the sidebar and try again.")
                else:
                    st.error("❌ Failed to generate timetable. The constraints are mathematically impossible.")
            except sqlite3.OperationalError as e:
                st.error(f"❌ **Database Error:** `{e}`")
            except Exception as e:
                st.error(f"❌ An unexpected error occurred: {e}")
else:
    st.markdown("👋 **Welcome to the Teacher Portal!** Select a tab below to view your schedule.")

st.divider()

if 'schedule' in st.session_state:
    st.markdown("### View & Export Schedules")
    tab_class, tab_teacher, tab_day = st.tabs(["📚 By Class", "👨‍🏫 By Teacher", "📅 By Day"])
    days_of_week = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
    
    # --- CLASS VIEW TAB ---
    with tab_class:
        class_options = st.session_state.get('classes', {})
        if class_options:
            c_col1, c_col2, c_col3 = st.columns([1.5, 2, 1])
            
            class_keys = list(class_options.keys())
            
            # --- Anti-Reset Memory Logic ---
            # Streamlit forgets widget state during double-reruns (like auto-generating). 
            # We store the selected ID in a permanent variable to force it to remember.
            if 'active_class_tab_id' not in st.session_state:
                st.session_state['active_class_tab_id'] = class_keys[0]
            if st.session_state['active_class_tab_id'] not in class_keys:
                st.session_state['active_class_tab_id'] = class_keys[0] # Fallback if class was deleted
                
            c_idx = class_keys.index(st.session_state['active_class_tab_id'])
            
            selected_class_id = c_col1.selectbox(
                "Select a Class:", 
                options=class_keys, 
                format_func=lambda x: class_options[x], 
                index=c_idx,
                key="view_class_dd_widget"
            )
            # Save the new selection into our permanent memory
            st.session_state['active_class_tab_id'] = selected_class_id
            
            search_query = c_col2.text_input("🔍 Highlight Teacher/Subject:", "", help="Type a teacher's or subject's name to instantly locate their periods.")
            
            schedule_data = st.session_state['schedule'][selected_class_id]
            table_data = []
            display_periods = st.session_state.get('num_periods', 8)
            for p in range(display_periods):
                row = {"Period": f"Period {p+1}"}
                for d in range(6):
                    row[days_of_week[d]] = schedule_data[d][p]
                table_data.append(row)
                
            df_class = pd.DataFrame(table_data)
            
            c_col3.write("") 
            c_col3.download_button(
                label=f"📥 Download CSV/Excel",
                data=df_class.to_csv(index=False).encode('utf-8'),
                file_name=f"Class_{class_options[selected_class_id]}_Timetable.csv",
                mime="text/csv",
                use_container_width=True
            )
            
            styled_df_class = df_class.style.map(lambda v: color_class_cells(v, search_query)) if hasattr(df_class.style, "map") else df_class.style.applymap(lambda v: color_class_cells(v, search_query))
            st.dataframe(styled_df_class, width='stretch', hide_index=True)
            
    # --- TEACHER VIEW TAB ---
    with tab_teacher:
        teacher_options = st.session_state.get('teachers', {})
        if teacher_options:
            t_col1, t_col2 = st.columns([3, 1])
            
            teacher_keys = list(teacher_options.keys())
            
            # --- Anti-Reset Memory Logic ---
            if 'active_teacher_tab_id' not in st.session_state:
                st.session_state['active_teacher_tab_id'] = teacher_keys[0]
            if st.session_state['active_teacher_tab_id'] not in teacher_keys:
                st.session_state['active_teacher_tab_id'] = teacher_keys[0]
                
            t_idx = teacher_keys.index(st.session_state['active_teacher_tab_id'])
            
            selected_teacher_id = t_col1.selectbox(
                "Select a Teacher:", 
                options=teacher_keys, 
                format_func=lambda x: teacher_options[x], 
                index=t_idx,
                key="view_teacher_dd_widget"
            )
            st.session_state['active_teacher_tab_id'] = selected_teacher_id
            
            selected_teacher_name = teacher_options[selected_teacher_id]
            table_data = []
            display_periods = st.session_state.get('num_periods', 8)
            
            assigned_count = 0
            for p in range(display_periods):
                row = {"Period": f"Period {p+1}"}
                for d in range(6):
                    assigned_classes_list = []
                    subject_name = ""
                    for class_id, class_schedule in st.session_state['schedule'].items():
                        cell_val = class_schedule[d][p]
                        if cell_val != "Free Period" and f"({selected_teacher_name})" in cell_val:
                            subjects_in_cell = cell_val.split(" / ")
                            for subj_teacher_combo in subjects_in_cell:
                                if f"({selected_teacher_name})" in subj_teacher_combo:
                                    subject_name = subj_teacher_combo.split(" (")[0]
                                    assigned_classes_list.append(st.session_state['classes'][class_id])
                                    break 
                    if assigned_classes_list:
                        assigned_class = f"{', '.join(assigned_classes_list)} - {subject_name}"
                        assigned_count += 1
                        row[days_of_week[d]] = assigned_class
                    else:
                        row[days_of_week[d]] = "Free Period"
                table_data.append(row)
                
            df_teacher = pd.DataFrame(table_data)
            
            t_col2.write("")
            t_col2.download_button(
                label=f"📥 Download CSV/Excel",
                data=df_teacher.to_csv(index=False).encode('utf-8'),
                file_name=f"Teacher_{selected_teacher_name}_Timetable.csv",
                mime="text/csv",
                use_container_width=True
            )
            
            conn = sqlite3.connect('school_data.db')
            t_rec = conn.execute("SELECT max_periods, part_time_limit FROM Teachers WHERE id=?", (selected_teacher_id,)).fetchone()
            conn.close()
            
            max_allowed = int(t_rec[0]) if t_rec and t_rec[0] is not None else 60
            pt_limit = int(t_rec[1]) if t_rec and len(t_rec)>1 and t_rec[1] is not None else 0
            
            total_slots = display_periods * 6
            if pt_limit > 0:
                total_slots = min(total_slots, pt_limit * 6)
                
            remaining_quota = max_allowed - assigned_count
            free_slots = total_slots - assigned_count
            
            st.markdown(f"**Workload Summary for {selected_teacher_name}:**")
            col_m1, col_m2, col_m3, col_m4 = st.columns(4)
            col_m1.metric("Max Allowed (Quota)", max_allowed)
            col_m2.metric("Currently Assigned", assigned_count)
            col_m3.metric("Remaining Quota", remaining_quota)
            col_m4.metric("Free Slots in Shift", free_slots)
            
            styled_df_teacher = df_teacher.style.map(lambda v: color_class_cells(v, "")) if hasattr(df_teacher.style, "map") else df_teacher.style.applymap(lambda v: color_class_cells(v, ""))
            st.dataframe(styled_df_teacher, width='stretch', hide_index=True)

    # --- DAY VIEW TAB ---
    with tab_day:
        if st.session_state.get('classes'):
            d_col1, d_col2 = st.columns([3, 1])
            
            # --- Anti-Reset Memory Logic ---
            if 'active_day_tab_name' not in st.session_state:
                st.session_state['active_day_tab_name'] = days_of_week[0]
                
            d_idx = days_of_week.index(st.session_state['active_day_tab_name'])
            
            selected_day_name = d_col1.selectbox(
                "Select a Day to View All Classes:", 
                options=days_of_week, 
                index=d_idx,
                key="view_day_dd_widget"
            )
            st.session_state['active_day_tab_name'] = selected_day_name
            
            selected_day_idx = days_of_week.index(selected_day_name)
            
            day_table_data = []
            display_periods = st.session_state.get('num_periods', 8)
            
            for c_id, c_name in st.session_state['classes'].items():
                row = {"Class": c_name}
                for p in range(display_periods):
                    row[f"Period {p+1}"] = st.session_state['schedule'][c_id][selected_day_idx][p]
                day_table_data.append(row)
                
            df_day = pd.DataFrame(day_table_data)
            
            d_col2.write("")
            d_col2.download_button(
                label=f"📥 Download {selected_day_name} CSV/Excel",
                data=df_day.to_csv(index=False).encode('utf-8'),
                file_name=f"{selected_day_name}_School_Timetable.csv",
                mime="text/csv",
                use_container_width=True
            )
            
            styled_df_day = df_day.style.map(lambda v: color_class_cells(v, "")) if hasattr(df_day.style, "map") else df_day.style.applymap(lambda v: color_class_cells(v, ""))
            st.dataframe(styled_df_day, width='stretch', hide_index=True)