# Rhythm integration tests (Report-5.2)

One test per test case of the sheets "Rhythm Evaluation API" (ITC-RE), "Rhythm Template Analysis" (ITC-RT-001..017),
"Rhythm Worker & Callback" (ITC-RW) and "Rhythm AI Feedback & Keys" (ITC-RK). The test name holds the test case ID.

What runs for real:

- the service itself (`python -m app.main`, API and worker in one process), started and restarted by the tests with
  the settings each case needs;
- PostgreSQL: a database named `bvc_rhythm_it` is dropped and created again at the start of every run, on the server
  of `RHYTHM_SERVICE_DATABASE_URL` in `.env` (or `RHYTHM_IT_DATABASE_URL`);
- RabbitMQ: `amqp://guest:guest@localhost:5672/` (or `RHYTHM_IT_RABBITMQ_URL`), with the management API on port 15672.
  The queues `rhythm.evaluate.v1`, `rhythm.template.analyze.v1` and their dead-letter queues are purged at the start.

A local HTTP stub plays the audio storage, the BE callback endpoint and Gemini (through `GOOGLE_GEMINI_BASE_URL`), so
no external service is called and no real API key is used.

## Run

```powershell
docker run -d --name bvc-rabbitmq-test -p 5672:5672 -p 15672:15672 rabbitmq:3.13-management
$env:RHYTHM_IT = "1"
.venv\Scripts\python.exe -m pytest tests/integration -p no:cacheprovider --junitxml=integration-results.xml
```

Without `RHYTHM_IT=1` the tests are skipped, so the normal unit test run does not need these services.
Set `RHYTHM_IT_KEEP=1` to keep the work folder (service logs, artifacts) printed at the start of the run.
A full run takes about 15 minutes; ITC-RW-008 and ITC-RW-009 are the slow ones.
