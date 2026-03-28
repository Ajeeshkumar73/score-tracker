import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, confusion_matrix, classification_report

df = pd.read_csv("productivity_10000.csv")

# Convert to categories
def categorize(score):
    if score < 40:
        return 0   # Low
    elif score < 70:
        return 1   # Medium
    else:
        return 2   # High

df["productivity_label"] = df["productivity_score"].apply(categorize)

X = df[[
    "total_tasks",
    "completed_tasks",
    "assigned_time_minutes",
    "actual_time_minutes"
]]

y = df["productivity_label"]

X_train, X_test, y_train, y_test = train_test_split(
    X, y, test_size=0.2, random_state=42
)

model = RandomForestClassifier(
    n_estimators=300,
    random_state=42
)

model.fit(X_train, y_train)

y_pred = model.predict(X_test)

# Metrics
acc = accuracy_score(y_test, y_pred)
cm = confusion_matrix(y_test, y_pred)

print("Accuracy:", round(acc*100, 4))
print("Confusion Matrix:\n", cm)
print("\nClassification Report:\n", classification_report(y_test, y_pred))