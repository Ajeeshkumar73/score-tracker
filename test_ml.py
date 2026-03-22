import joblib
import numpy as np

try:
    model = joblib.load("productivity_classifier_model.pkl")
    print("Model Loaded: productivity_classifier_model.pkl")
except:
    model = joblib.load("productivity_model.pkl")
    print("Model Loaded: productivity_model.pkl")

# Test 1: total 9, completed 5
# assigned_time and actual_time are also inputs. 
# If assigned_time is 9 * 1440 (if each task had 1 day) = 12960
# actual_time if each task took 1 hour = 5 * 60 = 300
features1 = np.array([[9, 5, 12960, 300]])
features2 = np.array([[9, 5, 1000, 1000]])
features3 = np.array([[9, 5, 5000, 4000]])

print(f"9 tasks, 5 completed, high buffer: {model.predict(features1)[0]}")
print(f"9 tasks, 5 completed, equal time: {model.predict(features2)[0]}")
print(f"9 tasks, 5 completed, long time: {model.predict(features3)[0]}")
