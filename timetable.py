from ortools.sat.python import cp_model

# Initialize the constraint solver model
model = cp_model.CpModel()

# Define the basic structure (6 working days, 8 periods a day)
num_days = 6 
num_periods = 8

# Define your classes
classes = ['Nursery', '10-A', '11-Science', '11-Commerce']
num_classes = len(classes)

# Define your faculty types
teachers = ['PRT Mother Teacher', 'TGT English', 'PGT Physics', 'PGT Accountancy']
num_teachers = len(teachers)

# Step 3: Create the Variables (The Grid)
schedule = {}
for t in range(num_teachers):
    for c in range(num_classes):
        for d in range(num_days):
            for p in range(num_periods):
                name = f'teacher_{t}_class_{c}_day_{d}_period_{p}'
                schedule[(t, c, d, p)] = model.NewBoolVar(name)

# Rule 1: A teacher can only be in ONE class at a time
for t in range(num_teachers):
    for d in range(num_days):
        for p in range(num_periods):
            model.AddAtMostOne(schedule[(t, c, d, p)] for c in range(num_classes))

# Rule 2: A class can only have ONE teacher per period
for c in range(num_classes):
    for d in range(num_days):
        for p in range(num_periods):
            model.AddAtMostOne(schedule[(t, c, d, p)] for t in range(num_teachers))

# Rule 3: Enforce the exact number of periods per week for assigned subjects
weekly_requirements = {
    (0, 0): 25, # PRT Mother Teacher teaches Nursery for 25 periods
    (1, 1): 6,  # TGT English teaches 10-A for 6 periods
    (2, 2): 8,  # PGT Physics teaches 11-Science for 8 periods
    (3, 3): 8   # PGT Accountancy teaches 11-Commerce for 8 periods
}

for (t, c), required_periods in weekly_requirements.items():
    all_slots_for_this_combo = []
    for d in range(num_days):
        for p in range(num_periods):
            all_slots_for_this_combo.append(schedule[(t, c, d, p)])
    
    # We tell the solver that the sum of these slots MUST equal the required periods
    model.Add(sum(all_slots_for_this_combo) == required_periods)

# Rule 4: Forbid invalid Teacher-Class combinations
for t in range(num_teachers):
    for c in range(num_classes):
        if (t, c) not in weekly_requirements:
            for d in range(num_days):
                for p in range(num_periods):
                    model.Add(schedule[(t, c, d, p)] == 0)

# Rule 5: Limit daily periods, but account for Mother Teachers!
for (t, c), required_periods in weekly_requirements.items():
    
    # If it is the PRT Mother Teacher (Index 0), allow up to 5 periods a day.
    # For all other subject teachers, limit them to 2 periods a day.
    if t == 0:
        max_daily = 5 
    else:
        max_daily = 2 
        
    for d in range(num_days):
        daily_slots = []
        for p in range(num_periods):
            daily_slots.append(schedule[(t, c, d, p)])
            
        # Enforce the specific daily limit for this teacher
        model.Add(sum(daily_slots) <= max_daily)
# Step 5: Solve and Print the Output
# Rule 6: Prevent predictable repetition (Avoid getting stuck in the same period)
for (t, c) in weekly_requirements.keys():
    
    # We skip the PRT Mother Teacher (Index 0) because she teaches 25 periods, 
    # meaning she HAS to be in the same periods almost every day.
    if t == 0:
        continue
        
    # For all other teachers, check across the whole week for a specific period
    for p in range(num_periods):
        same_period_slots = []
        for d in range(num_days):
            same_period_slots.append(schedule[(t, c, d, p)])
        
        # Force the solver: Do not assign this exact period index more than 2 times a week
        model.Add(sum(same_period_slots) <= 2)
# Rule 7: Science Labs must be Back-to-Back Double Periods
# For PGT Physics (Index 2) teaching 11-Science (Index 2)
t_lab = 2
c_lab = 2

# Create a "cheat sheet" menu of allowed daily patterns
valid_lab_patterns = []

# Option A: No Physics on this day (all 8 periods are empty)
valid_lab_patterns.append(tuple([0, 0, 0, 0, 0, 0, 0, 0]))

# Option B: Two consecutive periods somewhere in the day
for start_p in range(num_periods - 1):
    pattern = [0] * num_periods
    pattern[start_p] = 1
    pattern[start_p + 1] = 1
    valid_lab_patterns.append(tuple(pattern))
    
# Enforce this menu for every day of the week
for d in range(num_days):
    daily_vars = []
    for p in range(num_periods):
        daily_vars.append(schedule[(t_lab, c_lab, d, p)])
        
    # Tell the solver: "The schedule for this day MUST match one of the patterns on the menu"
    model.AddAllowedAssignments(daily_vars, valid_lab_patterns)
solver = cp_model.CpSolver()
status = solver.Solve(model)

if status == cp_model.OPTIMAL or status == cp_model.FEASIBLE:
    print("Success! Maharshi Dattatreya School Timetable Generated.")
    print("=" * 50)
    
    target_class = 2 # Index for 11-Science
    days_of_week = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
    
    print(f"\nWeekly Schedule for {classes[target_class]}:")
    
    for d in range(num_days):
        print(f"\n--- {days_of_week[d]} ---")
        for p in range(num_periods):
            assigned_teacher = "Free Period"
            for t in range(num_teachers):
                # Check which teacher was assigned to this specific slot
                if solver.Value(schedule[(t, target_class, d, p)]) == 1:
                    assigned_teacher = teachers[t]
            
            print(f"Period {p + 1}: {assigned_teacher}")
else:
    print("No solution found. Your constraints might be impossible to meet.")