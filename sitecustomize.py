# Intentionally empty.
#
# Railway starts the app through ai_entry.py, which loads main_compat and the
# optional diagnostics in a controlled order. Keeping sitecustomize passive
# avoids double registration of dashboard/API routes during Python startup.
