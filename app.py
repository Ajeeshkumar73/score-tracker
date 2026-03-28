import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, request, redirect, session, flash
from pymongo import MongoClient
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
from bson import ObjectId
import numpy as np
import joblib, os
from collections import defaultdict
from flask_mail import Mail, Message

from flask_socketio import SocketIO, emit, join_room


app = Flask(__name__)
app.secret_key = "secretkey"

socketio = SocketIO(app, cors_allowed_origins="*")


# Load ML model
try:
    model = joblib.load("productivity_model.pkl")
except:
    model = joblib.load("productivity_classifier_model.pkl")

# Session Configuration
# timedelta already imported above
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)
app.config['SESSION_REFRESH_EACH_REQUEST'] = True

@app.before_request
def make_session_permanent():
    session.permanent = True

# MongoDB
client = MongoClient("mongodb://localhost:27017/")
db = client.track_my_work
users = db.users
tasks = db.tasks
logs = db.activity_logs
scores = db.productivity_scores
chats = db.chats
notifications = db.notifications

#---mail---
app.config["MAIL_SERVER"] = "smtp.gmail.com"
app.config["MAIL_PORT"] = 587
app.config["MAIL_USE_TLS"] = True
app.config["MAIL_USERNAME"] = os.environ.get("MAIL_USERNAME")
app.config["MAIL_PASSWORD"] = os.environ.get("MAIL_PASSWORD")
app.config["MAIL_DEFAULT_SENDER"] = os.environ.get("MAIL_USERNAME")


mail = Mail(app)

def send_async_email(app, msg):
    with app.app_context():
        try:
            mail.send(msg)
        except Exception as e:
            print(f"Email error: {e}")

import threading


# ---------------- LANDING ----------------
@app.route("/")
def landing():
    return render_template("index.html")

# ---------------- AUTH ----------------
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        users.insert_one({
            "name": request.form["name"],
            "email": request.form["email"],
            "password": generate_password_hash(request.form["password"]),
            "role": request.form["role"],
            "position": request.form["position"]
        })
        flash("Registration successful! Please login.", "success")
        return redirect("/login")
    
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    # If already logged in, redirect to dashboard
    if "user_id" in session and "role" in session:
        return redirect("/manager" if session["role"]=="manager" else "/employee")

    if request.method == "POST":
        user = users.find_one({"email": request.form["email"]})
        if user and check_password_hash(user["password"], request.form["password"]):
            session.permanent = True
            session["user_id"] = str(user["_id"])
            session["role"] = user["role"]
            session["name"] = user["name"]
            flash("Welcome back!...Login successful")
            return redirect("/manager" if user["role"]=="manager" else "/employee")
        flash("Invalid email or password", "error")
        return render_template("login.html", error="Invalid credentials")
    
    return render_template("login.html")

# ---------------- MANAGER ----------------
@app.route("/manager")
def manager():
    if session.get("role") != "manager":
        return redirect("/login")
    
    manager_id = session["user_id"]
    user = users.find_one({"_id": ObjectId(manager_id)})

    all_tasks = list(tasks.find().sort("assigned_date", -1))
    employees = list(users.find({"role": "employee"}))
    

    latest_tasks = []
    for e in employees:
        emp_tasks = [t for t in all_tasks if t["employee_id"] == str(e["_id"])]

        if emp_tasks:
            latest = emp_tasks[0]  # latest because sorted desc
            latest["employee_name"] = e["name"]
            latest["total_tasks"] = len(emp_tasks)
            latest_tasks.append(latest)

    ranking = get_ranking()
    free_employees = get_free_employees()
    recommendations = get_smart_recommendations()
    # Derive weekly counts from recommendations (no extra DB queries needed)
    weekly_task_counts = {r["id"]: r["weekly_count"] for r in recommendations}

    unread_chats = chats.count_documents({
        "seen": False,
        "sender_role": "employee"
    })
    unread_notifs = notifications.count_documents({
        "user_id": manager_id,
        "seen": False
    })
    unread = unread_chats + unread_notifs

    pending_verification = list(tasks.find({"status": "pending_verification"}))
    for t in pending_verification:
        emp = users.find_one({"_id": ObjectId(t["employee_id"])})
        t["employee_name"] = emp["name"] if emp else "Unknown"

    return render_template(
        "manager.html",
        users=employees,
        user=user,
        recommendations=recommendations,
        unread=unread,
        free_employees=free_employees,
        tasks=latest_tasks,
        ranking=ranking,
        pending_verification=pending_verification,
        weekly_task_counts=weekly_task_counts,
        weekly_limit=WEEKLY_TASK_LIMIT
    )

@app.route("/employee_tasks/<emp_id>")
def employee_tasks(emp_id):
    if session.get("role") != "manager":
        return redirect("/login")

    emp = users.find_one({"_id": ObjectId(emp_id)})
    emp_tasks = list(tasks.find({"employee_id": emp_id}).sort("assigned_date", -1))

    total_tasks = len(emp_tasks)
    pending_tasks = sum(1 for t in emp_tasks if t["status"] != "completed")
    score = calculate_score(emp_id)

    return render_template(
        "employee_tasks.html",
        employee=emp,
        total_tasks=total_tasks,
        pending_tasks=pending_tasks,
        tasks=emp_tasks,
        score=score
    )


@app.route("/assign", methods=["POST"])
def assign():
    assigned_date = datetime.now()

    deadline = datetime.strptime(
        request.form["deadline"], "%Y-%m-%d"
    )

    days = (deadline - assigned_date).days
    assigned_minutes = days * 24 * 60

    employee_id = request.form["employee"]

    # get employee details
    employee = users.find_one({"_id": ObjectId(employee_id)})

    # ---- WEEKLY LIMIT CHECK ----
    weekly_count = count_weekly_tasks(employee_id)
    if weekly_count >= WEEKLY_TASK_LIMIT:
        flash(
            f"Cannot assign task — {employee['name']} has already received "
            f"{weekly_count} tasks this week (limit: {WEEKLY_TASK_LIMIT}). "
            f"Allow them to complete existing work first.",
            "error"
        )
        return redirect("/manager")
    # ----------------------------

    task_name = request.form["task"]
    category = request.form["category"]

    tasks.insert_one({
        "task_name": task_name,
        "category": category,
        "employee_id": employee_id,
        "manager_id": str(session["user_id"]),
        "assigned_date": assigned_date,
        "deadline": deadline,
        "assigned_time": assigned_minutes,
        "status": "assigned",
        "submit_date": None
    })

    # -------- PREPARE EMAIL --------
    msg = Message(
        subject="New Task Assigned",
        recipients=[employee["email"]]
    )

    msg.body = f"""
Hello {employee['name']},

A new task has been assigned to you.

Task: {task_name}
Category: {category}
Deadline: {deadline.strftime('%d-%m-%Y')}

Please login to view and accept:
http://127.0.0.1:5000/login

Regards,
Task Management System
"""

    # Run email sending in background to avoid delay
    threading.Thread(target=send_async_email, args=(app, msg)).start()

    # Update productivity score immediately on assignment
    calculate_productivity_ml(employee_id)

    remaining = WEEKLY_TASK_LIMIT - (weekly_count + 1)
    flash(
        f"Task assigned to {employee['name']}! "
        f"They can receive {remaining} more task(s) this week.",
        "success"
    )
    return redirect("/manager")



# ---------------- EMPLOYEE ----------------
@app.route("/employee")
def employee():
    if session.get("role") != "employee":
        return redirect("/login")

    emp_id = session["user_id"]

    user = users.find_one({"_id": ObjectId(emp_id)})

    my_tasks = list(tasks.find({"employee_id": emp_id}))

    my_tasks.sort(key=lambda t: t.get("assigned_date") or datetime.min, reverse=True)
    # ✅ THIS WAS MISSING
    my_task_ids = [t["_id"] for t in my_tasks]

    score = calculate_score(emp_id)

    total_tasks = len(my_tasks)
    completed = sum(1 for t in my_tasks if t["status"] == "completed")
    pending = sum(1 for t in my_tasks if t["status"] != "completed")

    unread_chats = chats.count_documents({
        "task_id": {"$in": my_task_ids},
        "seen": False,
        "sender_role": "manager"
    })
    unread_notifs = notifications.count_documents({
        "user_id": emp_id,
        "seen": False
    })
    unread = unread_chats + unread_notifs

    new_tasks = tasks.count_documents({
    "employee_id": emp_id,
    "status": "assigned"
    })


    return render_template(
        "employee.html",
        tasks=my_tasks,
        unread=unread,
        user=user,
        score=score,
        total_tasks=total_tasks,
        completed=completed,
        pending=pending,
        new_tasks=new_tasks
    )


@app.route("/accept/<task_id>")
def accept(task_id):
    if session.get("role") != "employee":
        return redirect("/login")

    emp_id = str(session["user_id"])   # FORCE STRING

    logs.insert_one({
        "employee_id": emp_id,
        "task_id": ObjectId(task_id),
        "start_time": datetime.now(),
        "end_time": None
    })

    tasks.update_one(
        {"_id": ObjectId(task_id)},
        {"$set": {"status": "in_progress"}}
    )

    return redirect("/employee")

@app.route("/submit/<task_id>")
def submit(task_id):
    if session.get("role") != "employee":
        return redirect("/login")

    task_oid = ObjectId(task_id)
    emp_id = str(session["user_id"])   # SAME TYPE

    log = logs.find_one({
        "employee_id": emp_id,
        "task_id": task_oid,
        "end_time": None
    })

    if not log:
        flash("Task was never accepted first!", "error")
        return redirect("/employee")

    end_time = datetime.now()
    duration = (end_time - log["start_time"]).total_seconds() / 60

    logs.update_one(
        {"_id": log["_id"]},
        {"$set": {
            "end_time": end_time,
            "duration": duration
        }}
    )

    tasks.update_one(
        {"_id": task_oid},
        {"$set": {
            "submit_date": end_time,
            "status": "pending_verification"
        }}
    )

    # Notification for manager
    task = tasks.find_one({"_id": task_oid})
    manager_id = task.get("manager_id")
    if manager_id:
        notifications.insert_one({
            "user_id": manager_id,
            "task_id": task_id,
            "type": "submission",
            "message": f"Employee {session.get('name', 'An employee')} submitted task: {task['task_name']}",
            "seen": False,
            "time": datetime.now()
        })
        socketio.emit("new_notification", {"task_id": task_id}, room=manager_id)

        # Email to manager
        manager = users.find_one({"_id": ObjectId(manager_id)})
        if manager:
            msg = Message(
                subject="Task Submitted for Verification",
                recipients=[manager["email"]]
            )
            msg.body = f"The task '{task['task_name']}' has been submitted for verification."
            threading.Thread(target=send_async_email, args=(app, msg)).start()

    flash("Task Submitted for verification")
    return redirect("/employee")

@app.route("/verify_task/<task_id>", methods=["POST"])
def verify_task(task_id):
    if session.get("role") != "manager":
        return redirect("/login")

    action = request.form.get("action")
    correction_msg = request.form.get("correction_msg", "")
    task_oid = ObjectId(task_id)

    task = tasks.find_one({"_id": task_oid})
    if not task:
        flash("Task not found", "error")
        return redirect("/manager")

    employee = users.find_one({"_id": ObjectId(task["employee_id"])})

    if action == "accept":
        # Calculate final duration if not set
        last_log = logs.find_one({"task_id": task_oid}, sort=[("end_time", -1)])
        duration = last_log["duration"] if last_log else 0

        tasks.update_one(
            {"_id": task_oid},
            {"$set": {
                "status": "completed",
                "completed_time": duration,
                "end_time": datetime.now()
            }}
        )
        calculate_productivity_ml(task["employee_id"])
        flash("Task accepted and marked as completed", "success")

        # Email to employee
        if employee:
            msg = Message(
                subject="Task Accepted",
                recipients=[employee["email"]]
            )
            msg.body = f"Your task '{task['task_name']}' has been accepted and completed."
            threading.Thread(target=send_async_email, args=(app, msg)).start()

    elif action == "reject":
        tasks.update_one(
            {"_id": task_oid},
            {"$set": {
                "status": "rejected",
                "correction_msg": correction_msg
            }}
        )
        
        # Notification for employee
        notifications.insert_one({
            "user_id": task["employee_id"],
            "task_id": task_id,
            "type": "rejection",
            "message": f"Manager {session.get('name', 'Manager')} rejected task: {task['task_name']}",
            "seen": False,
            "time": datetime.now()
        })
        socketio.emit("new_notification", {"task_id": task_id}, room=task["employee_id"])

        # Email to employee
        if employee:
            msg = Message(
                subject="Task Needs Correction",
                recipients=[employee["email"]]
            )
            msg.body = f"Your task '{task['task_name']}' has been rejected with the following note: {correction_msg}"
            threading.Thread(target=send_async_email, args=(app, msg)).start()

        flash("Task rejected with correction message", "info")

    return redirect("/manager")


# ---------------- ML LOGIC ----------------
def get_employee_features(employee_id):
    total_tasks = tasks.count_documents({"employee_id": employee_id})
    completed_tasks = tasks.count_documents({
        "employee_id": employee_id,
        "status": "completed"
    })

    log_list = list(logs.find({
        "employee_id": employee_id,
        "duration": {"$ne": None}
    }))

    actual_time = sum(l["duration"] for l in log_list)

    assigned_time = sum(
        float(t.get("assigned_time", 0))
        for t in tasks.find({"employee_id": employee_id})
    )

    return total_tasks, completed_tasks, assigned_time, actual_time

def calculate_productivity_ml(employee_id):
    total, completed, assigned_time, actual_time = get_employee_features(employee_id)

    print(f"DEBUG ML - Emp: {employee_id}, T: {total}, C: {completed}, AT: {assigned_time}, Act: {actual_time}")

    if total == 0:
        prediction = 0
    else:
        # Base completion rate (accounts for 60% of the score)
        completion_rate = (completed / total) * 100
        
        if completed == 0:
            prediction = completion_rate # which is 0
        else:
            features = np.array([[total, completed, assigned_time, actual_time]])
            # Model prediction (accounts for 40% of the score)
            try:
                ml_prediction = float(model.predict(features)[0])
            except:
                ml_prediction = completion_rate # Fallback

            # Hybrid Calculation: prevent inflated scores for low completion
            # If completion is 55% (5/9), the score shouldn't jump to 90
            prediction = (completion_rate * 0.7) + (ml_prediction * 0.3)
            
            # Efficiency Factor: If they did it significantly faster than assigned time
            if assigned_time > 0 and actual_time > 0:
                # If they used less than half the assigned time, it improves the score
                efficiency = assigned_time / actual_time
                if efficiency > 1.2:
                    prediction += 5 # Speed bonus
                elif efficiency < 0.8:
                    prediction -= 5 # Delayed penalty

    # Final Bounds
    prediction = max(0, min(100, prediction))

    scores.update_one(
        {"employee_id": employee_id},
        {"$set": {"score": round(float(prediction), 2)}},
        upsert=True
    )

def calculate_score(emp_id):
    doc = scores.find_one({"employee_id": emp_id})
    return doc["score"] if doc else 0

def get_ranking():
    employees = users.find({"role":"employee"})
    ranking = []
    for e in employees:
        ranking.append({
            "name": e["name"],
            "score": calculate_score(str(e["_id"]))
        })
    ranking = sorted(ranking, key=lambda x: x["score"], reverse=True)
    return ranking[:5]

# -------- WEEKLY LIMIT HELPERS --------
WEEKLY_TASK_LIMIT = 5

def get_week_start():
    """Return the start of the current ISO week (Monday 00:00:00)."""
    today = datetime.now()
    return today - timedelta(days=today.weekday(), hours=today.hour,
                             minutes=today.minute, seconds=today.second,
                             microseconds=today.microsecond)

def count_weekly_tasks(employee_id):
    """Count tasks assigned to an employee in the current week."""
    week_start = get_week_start()
    return tasks.count_documents({
        "employee_id": str(employee_id),
        "assigned_date": {"$gte": week_start}
    })

def is_weekly_limit_reached(employee_id):
    return count_weekly_tasks(employee_id) >= WEEKLY_TASK_LIMIT
# ---------------------------------------

def get_free_employees():
    busy_ids = tasks.distinct(
        "employee_id",
        {"status": "in_progress"}
    )

    free_users = users.find({
        "role": "employee",
        "_id": {"$nin": [ObjectId(i) for i in busy_ids]}
    })

    return list(free_users)[:5]

def get_smart_recommendations():
    """
    Returns employees ranked by a composite recommendation score.
    Uses 4 bulk MongoDB queries regardless of employee count (O(1) DB round-trips).

    Score formula:
      Productivity  40%  — ML-based historical performance
      Skill Fit     30%  — avg completion time in their strongest category
      Workload      20%  — remaining weekly task capacity
      Availability  10%  — currently free vs in-progress
    """
    all_employees = list(users.find({"role": "employee"}))
    if not all_employees:
        return []

    # ── 1. All productivity scores (one query) ──────────────────────────
    all_scores_map = {s["employee_id"]: float(s.get("score", 0))
                      for s in scores.find()}

    # ── 2. Currently busy employee IDs (one query) ──────────────────────
    busy_emp_ids = set(tasks.distinct("employee_id", {"status": "in_progress"}))

    # ── 3. Weekly task counts for all employees (one aggregation) ───────
    week_start = get_week_start()
    weekly_agg = tasks.aggregate([
        {"$match": {"assigned_date": {"$gte": week_start}}},
        {"$group": {"_id": "$employee_id", "count": {"$sum": 1}}}
    ])
    weekly_counts_map = {item["_id"]: item["count"] for item in weekly_agg}

    # ── 4. All category skill data (one aggregation across all employees) 
    cat_agg = list(logs.aggregate([
        {"$match": {"duration": {"$ne": None}}},
        {"$lookup": {
            "from": "tasks",
            "localField": "task_id",
            "foreignField": "_id",
            "as": "task_doc"
        }},
        {"$unwind": "$task_doc"},
        {"$group": {
            "_id": {
                "employee_id": "$employee_id",
                "category": "$task_doc.category"
            },
            "avg_time": {"$avg": "$duration"},
            "task_count": {"$sum": 1}
        }},
        {"$sort": {"avg_time": 1}}
    ]))

    # Group categories by employee (already sorted asc by avg_time)
    # Use .get() because older tasks may not have the 'category' field
    # and MongoDB may omit the key from the _id subdocument entirely.
    emp_categories = defaultdict(list)
    for item in cat_agg:
        emp_id   = item["_id"].get("employee_id")
        cat_name = item["_id"].get("category")   # None for tasks without category
        if not emp_id or not cat_name:            # skip orphaned / uncategorised logs
            continue
        emp_categories[emp_id].append({
            "category": cat_name,
            "avg_time": round(item["avg_time"], 1),
            "count":    item["task_count"]
        })

    # ── Build recommendation entries ────────────────────────────────────
    results = []
    for e in all_employees:
        emp_id = str(e["_id"])

        # Workload
        weekly_count = weekly_counts_map.get(emp_id, 0)
        at_limit     = weekly_count >= WEEKLY_TASK_LIMIT
        load_score   = round(((WEEKLY_TASK_LIMIT - min(weekly_count, WEEKLY_TASK_LIMIT))
                               / WEEKLY_TASK_LIMIT) * 100, 1)

        # Productivity
        prod_score = round(all_scores_map.get(emp_id, 0.0), 1)

        # Availability
        is_free    = emp_id not in busy_emp_ids
        avail_score = 100.0 if is_free else 40.0

        # Skill fit — 240 min (4 hrs) used as baseline; faster → higher score
        cat_skills = emp_categories.get(emp_id, [])
        if cat_skills:
            best          = cat_skills[0]
            best_category = best.get("category") or "other"
            avg_time      = best["avg_time"]
            skill_score   = round(max(0.0, min(100.0, 100 - (avg_time / 240 * 100))), 1)
        else:
            best_category = ""
            avg_time      = None
            skill_score   = 50.0   # neutral when no history

        # Composite score
        composite = (
            prod_score  * 0.40 +
            skill_score * 0.30 +
            load_score  * 0.20 +
            avail_score * 0.10
        )
        rec_score = round(min(100.0, max(0.0, composite)), 1)

        results.append({
            "id":               emp_id,
            "name":             e["name"],
            "position":         e.get("position", ""),
            "best_category":    best_category,
            "avg_time":         avg_time,
            "category_skills":  cat_skills,
            "weekly_count":     weekly_count,
            "at_limit":         at_limit,
            "load_score":       load_score,
            "prod_score":       prod_score,
            "is_free":          is_free,
            "skill_score":      skill_score,
            "recommendation_score": rec_score
        })

    # Best match first
    results.sort(key=lambda x: x["recommendation_score"], reverse=True)
    return results

@app.route("/auto_assign/<emp_id>", methods=["POST"])
def auto_assign(emp_id):
    user = users.find_one({"_id": ObjectId(emp_id)})

    # ---- WEEKLY LIMIT CHECK ----
    weekly_count = count_weekly_tasks(emp_id)
    if weekly_count >= WEEKLY_TASK_LIMIT:
        flash(
            f"Cannot assign task — {user['name']} has already received "
            f"{weekly_count} tasks this week (limit: {WEEKLY_TASK_LIMIT}). "
            f"Allow them to complete existing work first.",
            "error"
        )
        return redirect("/manager")
    # ----------------------------

    assigned_date = datetime.now()
    deadline = datetime.strptime(request.form["deadline"], "%Y-%m-%d")
    days = (deadline - assigned_date).days
    assigned_minutes = max(0, days * 24 * 60)

    tasks.insert_one({
        "task_name": request.form["task"],
        "category": request.form["category"],
        "employee_id": emp_id,
        "employee_name": user["name"],
        "manager_id": str(session["user_id"]),
        "assigned_date": assigned_date,
        "deadline": deadline,
        "assigned_time": assigned_minutes,
        "status": "assigned"
    })

    # Update productivity score immediately on assignment
    calculate_productivity_ml(emp_id)

    remaining = WEEKLY_TASK_LIMIT - (weekly_count + 1)
    flash(
        f"Task auto-assigned to {user['name']}! "
        f"They can receive {remaining} more task(s) this week.",
        "success"
    )
    return redirect("/manager")

@app.route("/get_notifications")
def get_notifications():
    if "user_id" not in session:
        return {"error": "Unauthorized"}, 401
    
    user_id = session["user_id"]
    role = session["role"]
        
    user_notifs = list(notifications.find({
        "user_id": user_id,
        "seen": False
    }).sort("time", -1))
    
    # Also fetch unread chats
    if role == "manager":
        unread_chats = list(chats.find({
            "seen": False,
            "sender_role": "employee"
        }).sort("time", -1))
    else:
        # Get employee's own tasks
        my_tasks = tasks.find({"employee_id": user_id})
        task_ids = [str(t["_id"]) for t in my_tasks]
        unread_chats = list(chats.find({
            "task_id": {"$in": task_ids},
            "seen": False,
            "sender_role": "manager"
        }).sort("time", -1))

    # Transform into clean JSON structure
    combined = []
    
    for c in unread_chats:
        combined.append({
            "_id": str(c["_id"]),
            "task_id": str(c.get("task_id", "")),
            "type": "chat",
            "message": f"Message from {c.get('sender', 'Someone')}: {c.get('message', '')[:30]}...",
            "time": c["time"].strftime("%H:%M") if "time" in c and hasattr(c["time"], "strftime") else ""
        })

    for n in user_notifs:
        combined.append({
            "_id": str(n["_id"]),
            "task_id": str(n.get("task_id", "")),
            "type": n.get("type", "general"),
            "message": n.get("message", "New notification"),
            "time": n["time"].strftime("%H:%M") if "time" in n and hasattr(n["time"], "strftime") else ""
        })

    return {"notifications": combined}

@app.route("/mark_seen/<notif_id>", methods=["POST"])
def mark_seen(notif_id):
    # Try updating in both collections
    notif_result = notifications.update_one(
        {"_id": ObjectId(notif_id)},
        {"$set": {"seen": True}}
    )
    
    if notif_result.matched_count == 0:
        chats.update_one(
            {"_id": ObjectId(notif_id)},
            {"$set": {"seen": True}}
        )
        
    return {"status": "success"}

#-----------chat----------------
@app.route("/chat/<task_id>")
def chat(task_id):
    if "user_id" not in session:
        return redirect("/login")

    task = tasks.find_one({"_id": ObjectId(task_id)})
    messages = list(chats.find({"task_id": task_id}))

    employee = users.find_one({"_id": ObjectId(task["employee_id"])})

    # Mark messages as seen if they were sent by the OTHER role
    my_role = session["role"]
    other_role = "manager" if my_role == "employee" else "employee"
    
    chats.update_many(
        {"task_id": task_id, "sender_role": other_role, "seen": False},
        {"$set": {"seen": True}}
    )

    # Mark notifications as seen for this task
    notifications.update_many(
        {"user_id": session["user_id"], "task_id": task_id, "seen": False},
        {"$set": {"seen": True}}
    )

    return render_template(
        "chat.html",
        task=task,
        messages=messages,
        employee=employee
    )


@socketio.on("join")
def on_join(data):
    join_room(data["task_id"])


@socketio.on("join_user")
def join_user(data):
    join_room(data["user_id"])


@socketio.on("send_message")
def handle_message(data):
    chats.insert_one({
        "task_id": data["task_id"],
        "sender": data["sender"],
        "sender_role": data["role"],
        "message": data["message"],
        "time": datetime.now(),
        "seen": False
    })

    emit("receive_message", {
        "sender_role": data["role"],
        "message": data["message"]
    }, room=data["task_id"])

    # create notification
    task = tasks.find_one({"_id": ObjectId(data["task_id"])})

    receiver = (
        task["employee_id"]
        if data["role"] == "manager"
        else task.get("manager_id")
    )

    if receiver:
        receiver = str(receiver)

        notifications.insert_one({
            "user_id": receiver,
            "task_id": data["task_id"],
            "type": "chat",
            "message": f"New message from {data['sender']}",
            "seen": False,
            "time": datetime.now()
        })

        emit("new_notification", {"task_id": data["task_id"]}, room=receiver)


# ---------------- LOGOUT ----------------
@app.route("/logout")
def logout():
    session.clear()
    flash("Logout Successfully")
    return redirect("/")

if __name__ == "__main__":
    socketio.run(app, debug=True)
