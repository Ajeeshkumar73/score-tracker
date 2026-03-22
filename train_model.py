import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import r2_score
import joblib

df = pd.read_csv("productivity_10000.csv")

X = df[[
    "total_tasks",
    "completed_tasks",
    "assigned_time_minutes",
    "actual_time_minutes"
]]

y = df["productivity_score"]

X_train, X_test, y_train, y_test = train_test_split(
    X, y, test_size=0.2, random_state=42
)

model = RandomForestRegressor(
    n_estimators=300,
    random_state=42
)

model.fit(X_train, y_train)

y_pred = model.predict(X_test)
accuracy = r2_score(y_test, y_pred)
print("Model R2 Accuracy:", round(accuracy, 4))

joblib.dump(model, "productivity_model.pkl")
print("Model saved")
