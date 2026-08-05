import sqlite3

def setup_database():
    # 1. Connect to SQLite (This will create 'school_data.db' in your folder if it doesn't exist)
    conn = sqlite3.connect('school_data.db')
    cursor = conn.cursor()

    print("Connected to SQLite Database.")

    # 2. Create the Tables
    # Teachers Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Teachers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            designation TEXT NOT NULL,
            specialization TEXT,
            max_periods INTEGER
        )
    ''')

    # Classes Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Classes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            grade_section TEXT NOT NULL,
            stage TEXT -- e.g., Foundational, Middle, Secondary
        )
    ''')

    # Subjects Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Subjects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            type TEXT NOT NULL -- e.g., Core, Lab, Activity
        )
    ''')

    # Workloads Table (This links Teachers, Classes, and Subjects together)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Workloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            teacher_id INTEGER,
            class_id INTEGER,
            subject_id INTEGER,
            periods_per_week INTEGER,
            FOREIGN KEY(teacher_id) REFERENCES Teachers(id),
            FOREIGN KEY(class_id) REFERENCES Classes(id),
            FOREIGN KEY(subject_id) REFERENCES Subjects(id)
        )
    ''')

    print("Successfully created tables: Teachers, Classes, Subjects, Workloads.")

    # 3. Clear existing data (useful if you run this script multiple times while testing)
    cursor.execute('DELETE FROM Workloads')
    cursor.execute('DELETE FROM Teachers')
    cursor.execute('DELETE FROM Classes')
    cursor.execute('DELETE FROM Subjects')

    # 4. Insert Sample Data
    # Insert Teachers
    teachers_data = [
        ('Mrs. Sharma', 'PRT', 'General', 30),
        ('Mr. Gupta', 'TGT', 'English', 24),
        ('Dr. Verma', 'PGT', 'Physics', 24),
        ('Mr. Singh', 'PGT', 'Accountancy', 24)
    ]
    cursor.executemany('INSERT INTO Teachers (name, designation, specialization, max_periods) VALUES (?, ?, ?, ?)', teachers_data)

    # Insert Classes
    classes_data = [
        ('Nursery', 'Foundational'),
        ('10-A', 'Secondary'),
        ('11-Science', 'Senior Secondary'),
        ('11-Commerce', 'Senior Secondary')
    ]
    cursor.executemany('INSERT INTO Classes (grade_section, stage) VALUES (?, ?)', classes_data)

    # Insert Subjects
    subjects_data = [
        ('Foundational Studies', 'Core'),
        ('English Language', 'Core'),
        ('Physics (Theory & Lab)', 'Lab'),
        ('Accountancy', 'Core')
    ]
    cursor.executemany('INSERT INTO Subjects (name, type) VALUES (?, ?)', subjects_data)

    # Insert Workloads (Linking them: Mrs. Sharma teaches Nursery Foundational for 25 periods)
    workloads_data = [
        (1, 1, 1, 25), # Teacher 1, Class 1, Subject 1, 25 periods
        (2, 2, 2, 6),  # Teacher 2, Class 2, Subject 2, 6 periods
        (3, 3, 3, 8),  # Teacher 3, Class 3, Subject 3, 8 periods
        (4, 4, 4, 8)   # Teacher 4, Class 4, Subject 4, 8 periods
    ]
    cursor.executemany('INSERT INTO Workloads (teacher_id, class_id, subject_id, periods_per_week) VALUES (?, ?, ?, ?)', workloads_data)

    print("Sample data inserted successfully.")

    # 5. Verify the data with a JOIN query
    print("\n--- Current Workload Assignments in Database ---")
    cursor.execute('''
        SELECT Teachers.name, Classes.grade_section, Subjects.name, Workloads.periods_per_week 
        FROM Workloads
        JOIN Teachers ON Workloads.teacher_id = Teachers.id
        JOIN Classes ON Workloads.class_id = Classes.id
        JOIN Subjects ON Workloads.subject_id = Subjects.id
    ''')
    
    rows = cursor.fetchall()
    for row in rows:
        print(f"Teacher: {row[0]} | Class: {row[1]} | Subject: {row[2]} | Periods: {row[3]}")

    # Commit changes and close connection
    conn.commit()
    conn.close()

if __name__ == '__main__':
    setup_database()