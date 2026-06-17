## Architecture Sketch

<img width="1448" height="1086" alt="image" src="https://github.com/user-attachments/assets/c922e821-2627-4143-b8d6-c982dc1ed456" />

**Implemntation:**

`run_models.py` creates DuckDB views in order, then exports the reconciliation mart as JSON. The mart is the handoff point between SQL modeling and the agent, and the agent doesn't query the database directly; it reads the snapshot file. This is a deliberate tradeoff explained in Section 3.

The audit agent makes three independent LLM calls per anomaly one to detect, one to verify against raw data, one to generate SQL

## Trade-offs

**1. The join uses a 5-minute time window, not the direct foreign key**

The data has `ext_id` is a direct link from server transactions back to client events. I didn't use it as the primary join in `int_user_journey.sql`. In production I'd flip this `ext_id` as primary, time-window as fallback for transactions with no client link

**2. SQL files instead of a dbt project**

Five standalone `.sql` files, each with a grain and limitation block in the header, verified by running them as DuckDB views. No `dbt_project.yml`, no schema tests, no CI.

The files are production-quality SQL. The scaffolding is not. Adding dbt wiring takes maybe 90 minutes and would have taken out time better spent on modeling decisions and the agent logic

Five standalone .sql files were delivered, and each was verified by running it as a DuckDB view.

The SQL is production-quality but there is no dbt_project.yml, no schema tests, and no CI setup.

Adding the dbt wiring would likely take about 90 minutes. For this task, that time was better spent on the modeling decisions and the agent logic, which were the higher-value parts of the work
