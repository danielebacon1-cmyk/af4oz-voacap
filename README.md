# AF4OZ VOACAP test service

Place app.py and Dockerfile at the root of a Git repository. For a public repository, select Public Git Repository in Render and paste the repository URL. Select Docker and the Free instance for the test. Use /health as the health check path.

After deployment, open /health on the resulting service URL and test /forecast?origin=EM75LD&grid=KM72&year=2026&month=11&hour=17&ssn=100.

This is a prototype for monthly VOACAP SSB forecasts at a specified UTC hour. The example uses an illustrative sunspot number of 100. It returns short and long paths and the lowest tested frequency meeting 90% reliability, when any qualifies. It does not include the homepage dashboard or NOAA feed. The engine was verified locally. Render deployment has not been tested.
