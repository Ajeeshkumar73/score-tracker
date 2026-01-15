from flask import Flask, render_template, request, redirect, session
from pymongo import MongoClient
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from bson import ObjectId, SON

app = Flask(__name__)
app.secret_key = "secretkey"

# MongoDB
client = MongoClient("mongodb://localhost:27017/")
db = client.track_my_work
users = db.users

@app.route("/")
def landing():
    return render_template("index.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        role = request.form.get("role")

        if role not in ["manager", "employee"]:
            return "Invalid role", 400

        user = {
            "name": request.form["name"],
            "email": request.form["email"],
            "password": generate_password_hash(request.form["password"]),
            "role": role
        }
        users.insert_one(user)
        return redirect("/login")

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = users.find_one({"email": request.form["email"]})

        if user and check_password_hash(user["password"], request.form["password"]):
            session["user_id"] = str(user["_id"])
            session["role"] = user["role"]

            return redirect("/manager" if user["role"] == "manager" else "/employee")

        return render_template("login.html", error="Invalid email or password")

    return render_template("login.html")


@app.route("/employee")
def employee_dashboard():
    if session.get("role") != "employee":
        return redirect("/login")

    user_id = session["user_id"]

    # Fetch tasks
    tasks = list(db.tasks.find({"employee_id": user_id}))

    # Fetch productivity score
    score_doc = db.productivity_scores.find_one({"employee_id": user_id})
    score = score_doc["score"] if score_doc else 0

    # Simple task recommendation logic
    pending_task = db.tasks.find_one(
        {"employee_id": user_id, "status": "Pending"},
        sort=[("priority", -1)]
    )
    recommendation = pending_task["title"] if pending_task else "No pending tasks 🎉"

    return render_template(
        "employee_dashboard.html",
        tasks=tasks,
        score=score,
        recommendation=recommendation
    )

@app.route("/start-task/<task_id>")
def start_task(task_id):
    db.tasks.update_one(
        {"_id": ObjectId(task_id)},
        {"$set": {"status": "In Progress"}}
    )

    db.activity_logs.insert_one({
        "employee_id": session["user_id"],
        "task_id": task_id,
        "start_time": datetime.now(),
        "end_time": None,
        "duration": None
    })

    return redirect("/employee")

@app.route("/end-task/<task_id>")
def end_task(task_id):
    log = db.activity_logs.find_one({
        "employee_id": session["user_id"],
        "task_id": task_id,
        "end_time": None
    })

    end_time = datetime.now()
    duration = (end_time - log["start_time"]).seconds // 60

    db.activity_logs.update_one(
        {"_id": log["_id"]},
        {"$set": {"end_time": end_time, "duration": duration}}
    )

    db.tasks.update_one(
        {"_id": ObjectId(task_id)},
        {"$set": {"status": "Completed"}}
    )

    return redirect("/employee")

def calculate_productivity(employee_id):
    completed = db.tasks.count_documents({
        "employee_id": employee_id,
        "status": "Completed"
    })

    total_time = sum(
        log["duration"] for log in db.activity_logs.find(
            {"employee_id": employee_id, "duration": {"$ne": None}}
        )
    )

    score = min(100, completed * 10 + total_time // 30)

    db.productivity_scores.update_one(
        {"employee_id": employee_id},
        {"$set": {"score": score}},
        upsert=True
    )

@app.route("/manager")
def manager_dashboard():
    if session.get("role") != "manager":
        return redirect("/login")

    employees = list(db.users.find({"role": "employee"}))

    productivity = []
    workload = []

    for emp in employees:
        score_doc = db.productivity_scores.find_one({"employee_id": str(emp["_id"])})
        score = score_doc["score"] if score_doc else 0

        task_count = db.tasks.count_documents({"employee_id": str(emp["_id"])})

        productivity.append({
            "name": emp["name"],
            "score": score
        })

        workload.append({
            "name": emp["name"],
            "task_count": task_count
        })

    top_performers = sorted(productivity, key=lambda x: x["score"], reverse=True)[:3]
    low_performers = sorted(productivity, key=lambda x: x["score"])[:3]

    return render_template(
        "manager_dashboard.html",
        employees=employees,
        productivity=productivity,
        workload=workload,
        top_performers=top_performers,
        low_performers=low_performers
    )

@app.route("/create-task", methods=["POST"])
def create_task():
    if session.get("role") != "manager":
        return redirect("/login")

    db.tasks.insert_one({
        "title": request.form["title"],
        "employee_id": request.form["employee_id"],
        "priority": request.form["priority"],
        "deadline": request.form["deadline"],
        "status": "Pending"
    })

    return redirect("/manager")

def generate_report():
    return {
        "total_tasks": db.tasks.count_documents({}),
        "completed_tasks": db.tasks.count_documents({"status": "Completed"}),
        "avg_productivity": sum(
            p["score"] for p in db.productivity_scores.find()
        ) / max(1, db.productivity_scores.count_documents({}))
    }


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

if __name__ == "__main__":
    app.run(debug=True)
