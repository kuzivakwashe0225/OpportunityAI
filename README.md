# Opportunity Agent

Scholarship Scout is the first MVP slice of Opportunity Agent. It is read-only: it collects scholarship opportunities, checks them against an evidence-backed personal profile, and prepares a digest. It does not log in, upload, pay, send, or submit.

## Development

```powershell
C:/Users/Isaia/AppData/Local/Python/pythoncore-3.14-64/python.exe -m pip install -e ".[test]"
C:/Users/Isaia/AppData/Local/Python/pythoncore-3.14-64/python.exe -m pytest
C:/Users/Isaia/AppData/Local/Python/pythoncore-3.14-64/python.exe -m uvicorn opportunity_agent.api:app --reload
```

The initial test suite covers the matching rules, unknown eligibility information, duplicate evidence merging, and the API health check.
