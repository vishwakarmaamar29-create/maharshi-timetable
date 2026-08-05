import sqlite3
from ortools.sat.python import cp_model

def generate_timetable(num_periods=8):
    # 1. Connect to Database and Fetch Data
    conn = sqlite3.connect('school_data.db')
    cursor = conn.cursor()

    # Fetch Teachers (ID, Name, Designation)
    cursor.execute("SELECT id, name, designation FROM Teachers")
    teachers_data = cursor.fetchall()
    teacher_ids = [row[0] for row in teachers_data]
    teacher_names = {row[0]: row[1] for row in teachers_data}
    teacher_designations = {row[0]: row[2] for row in teachers_data}

    # Fetch Classes (ID, Name)
    cursor.execute("SELECT id, grade_section FROM Classes")
    classes_data = cursor.fetchall()
    class_ids = [row[0] for row in classes_data]
    class_names = {row[0]: row[1] for row in classes_data}
    
    # Fetch Subjects (ID, Name)
    cursor.execute("SELECT id, name FROM Subjects")
    subjects_data = cursor.fetchall()
    subject_names = {row[0]: row[1] for row in subjects_data}

    # Fetch Workloads (Teacher ID, Class ID, Subject ID, Periods)
    cursor.execute("SELECT teacher_id, class_id, subject_id, periods_per_week FROM Workloads")
    workloads_data = cursor.fetchall()
    
    # Format workloads into a dictionary: (teacher, class, subject) -> periods
    weekly_requirements = {(row[0], row[1], row[2]): row[3] for row in workloads_data}

    conn.close()

    # 2. Setup OR-Tools Model
    model = cp_model.CpModel()
    num_days = 6

    # 3. Create Variables (The Grid)
    schedule = {}
    # Optimization: Only create grid variables for valid Teacher/Class/Subject combos that exist in the DB!
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
                # Grab all active subjects this teacher is teaching this period across all classes
                model.AddAtMostOne(schedule[(t, c, s, d, p)] for (tc, c, s) in weekly_requirements.keys() if tc == t)

    # A class can only have one teacher/subject per period
    for c in class_ids:
        for d in range(num_days):
            for p in range(num_periods):
                # Grab all teachers/subjects assigned to this class this period
                model.AddAtMostOne(schedule[(t, cc, s, d, p)] for (t, cc, s) in weekly_requirements.keys() if cc == c)

    # Enforce exact periods per week based on Database Workloads
    for (t, c, s), required_periods in weekly_requirements.items():
        model.Add(sum(schedule[(t, c, s, d, p)] for d in range(num_days) for p in range(num_periods)) == required_periods)

    # 5. Smart Constraints
    # Limit daily periods dynamically based on whether they are a PRT/Mother Teacher or PGT/TGT
    for t in teacher_ids:
        if t not in teacher_designations:
            continue
            
        designation = teacher_designations[t]
        max_daily = 5 if designation in ['PRT', 'Mother Teacher'] else 2
        
        # Get all class/subject workloads for this specific teacher
        t_workloads = [(c, s) for (tc, c, s) in weekly_requirements.keys() if tc == t]
        if not t_workloads:
            continue
            
        for d in range(num_days):
            model.Add(sum(schedule[(t, c, s, d, p)] for (c, s) in t_workloads for p in range(num_periods)) <= max_daily)

    # Avoid same period repetition (Skip PRT and Mother Teachers)
    for (t, c, s) in weekly_requirements.keys():
        if teacher_designations[t] in ['PRT', 'Mother Teacher']:
            continue
        for p in range(num_periods):
            model.Add(sum(schedule[(t, c, s, d, p)] for d in range(num_days)) <= 2)

    # --- SAFETY FIX: Back-to-Back Labs ---
    # Only enforce this rule if Teacher 3 and Class 3 STILL exist in the database!
    t_lab, c_lab = 3, 3 
    # Check if this combination is in workloads and grab the subject id
    lab_combos = [s for (tc, cc, s) in weekly_requirements.keys() if tc == t_lab and cc == c_lab]
    
    if lab_combos:
        s_lab = lab_combos[0] # apply to the first mapped subject for this teacher/class
        valid_lab_patterns = [tuple([0]*num_periods)]
        for start_p in range(num_periods - 1):
            pattern = [0] * num_periods
            pattern[start_p] = 1
            pattern[start_p + 1] = 1
            valid_lab_patterns.append(tuple(pattern))
            
        for d in range(num_days):
            daily_vars = [schedule[(t_lab, c_lab, s_lab, d, p)] for p in range(num_periods)]
            model.AddAllowedAssignments(daily_vars, valid_lab_patterns)

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
                    
                    # Search valid combinations to see who is teaching this class right now
                    for (t, cc, s) in weekly_requirements.keys():
                        if cc == c and solver.Value(schedule[(t, c, s, d, p)]) == 1:
                            # Format Output as: "Subject Name (Teacher Name)"
                            assigned_value = f"{subject_names[s]} ({teacher_names[t]})"
                    
                    master_schedule[c][d][p] = assigned_value
                    
        return master_schedule, class_names, teacher_names
    else:
        return None, None, None