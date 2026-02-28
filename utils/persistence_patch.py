import logging
import uuid
from telegram.ext import Job, JobQueue
from telegram.ext._utils.types import CCT
from apscheduler.job import Job as APSJob
from apscheduler.util import undefined

_logger = logging.getLogger(__name__)
_current_app = None


async def _patched_job_callback(job: "Job[CCT]") -> None:
    """
    This is a replacement for the default job_callback.
    It's designed to work with persistent jobs. It retrieves the
    application instance from a global reference and re-links the job to its
    APScheduler counterpart if it was loaded from persistence.
    """
    global _current_app
    if not _current_app:
        _logger.error("Persistence patch error: _current_app not set. Cannot run job.")
        return

    # For jobs loaded from persistence, job._job is None. We need to re-link it.
    if not job._job:
        scheduler = _current_app.job_queue.scheduler
        # We rely on the job having a unique ID stored in its data to find it.
        job_id = None
        if isinstance(job.data, dict):
            job_id = job.data.get('_persistence_id')

        if not job_id:
            _logger.error(
                "Could not find persistence ID for PTB Job '%s'. Cannot re-link job. "
                "This can happen if job.data is not a dictionary.",
                job.name
            )
            return

        aps_job = scheduler.get_job(job_id)
        if not aps_job:
            _logger.warning(
                f"Could not find matching APSJob with id {job_id} "
                f"for PTB Job '{job.name}'. The job may have been removed. Skipping."
            )
            return
        job._job = aps_job

    await job.run(_current_app)


def apply_persistence_patch(application):
    """
    Applies a patch to the application's job queue to allow persistence
    with python-telegram-bot v20+ and apscheduler v3.
    """
    global _current_app
    _current_app = application

    job_queue = application.job_queue
    scheduler = job_queue.scheduler

    # Patch the scheduler's add_job method to intercept job creation.
    original_add_job = scheduler.add_job

    def patched_add_job(func, trigger=None, args=None, kwargs=None, id=None, name=None,
                        misfire_grace_time=undefined, coalesce=undefined, max_instances=undefined,
                        next_run_time=undefined, jobstore='default', executor='default',
                        replace_existing=False, **trigger_args):
        if func == JobQueue.job_callback:
            ptb_job = args[1]
            new_args = (ptb_job,)

            # Ensure the job has a unique ID and store it in the job's data dict.
            job_id = id or str(uuid.uuid4())

            if ptb_job.data is None:
                ptb_job.data = {}

            if isinstance(ptb_job.data, dict):
                ptb_job.data['_persistence_id'] = job_id
            else:
                _logger.warning(
                    "Job '%s' has non-dict data, which is not supported by the persistence patch. "
                    "The job may not be restored correctly after a restart.",
                    ptb_job.name
                )

            aps_job = original_add_job(
                _patched_job_callback, trigger, new_args, kwargs, job_id, name,
                misfire_grace_time, coalesce, max_instances, next_run_time,
                jobstore, executor, replace_existing, **trigger_args
            )
            # Link the created APSJob back to the PTB Job for live operations.
            ptb_job._job = aps_job
            return aps_job

        return original_add_job(
            func, trigger, args, kwargs, id, name,
            misfire_grace_time, coalesce, max_instances, next_run_time,
            jobstore, executor, replace_existing, **trigger_args
        )

    scheduler.add_job = patched_add_job