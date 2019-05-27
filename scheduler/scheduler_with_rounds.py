from __future__ import print_function

import heapq
import numpy as np
import os
from preconditions import preconditions
import queue
import sys
import threading
import time
import datetime

import job_id_pair
import job_queue
from runtime.rpc import scheduler_server, scheduler_client
import utils

np.random.seed(42)

SCHEDULER_PORT = 50060
SLEEP_SECONDS = 2
INFINITY = float(1e9)
DEFAULT_THROUGHPUT = INFINITY
DEFAULT_NUM_STEPS = 100     # Default number of steps in each iteration.
# TODO: Changed this to 5 minutes so that the case where applications don't
# fully use their time quantum is rare.
# TODO: Figure out a more sustainable solution to this, since 1 minute is too
# short (will incur appreciable context switch overhead).
TIME_PER_ITERATION = 5 * 60    # Time in seconds each iteration should run for.
EMA_ALPHA = .25 # Alpha parameter for exponential moving average.
MAX_FAILED_ATTEMPTS = 5

class Scheduler:

    def __init__(self, policy, job_packing=False, emulate=False,
                 throughputs_file=None):

        # Flag to control whether scheduler runs in emulation mode.
        self._emulate = emulate
        # Latest emulated timestamp.
        self._current_timestamp = 0
        # Start and last processed timestamp for each job_id.
        self._per_job_start_timestamps = {}
        self._per_job_latest_timestamps = {}
        # Job completion times.
        self._job_completion_times = {}
        # Queue of events that need to be processed at specific timestamps.
        self._event_queue = []

        # List of worker IDs.
        self._worker_ids = []
        # List of worker types.
        self._worker_types = set()
        # Mapping of worker ID to worker type, and worker type to worker ID.
        self._worker_id_to_worker_type_mapping = {}
        self._worker_type_to_worker_id_mapping = {}
        # Policy instance.
        self._policy = policy
        self._job_packing = job_packing
        # RPC clients.
        self._cluster_spec = {}
        self._worker_connections = {}
        # Next job_id to assign.
        self._job_id_counter = 0
        # Next worker_id to assign.
        self._worker_id_counter = 0
        # Lock to ensure worker_id assignment is thread-safe.
        self._scheduler_lock = threading.Lock()
        # List of available worker IDs.
        self._available_worker_ids = queue.Queue(0)
        # Throughputs for all current incomplete applications.
        self._throughputs = {}
        # Allocations for all current incomplete applications.
        self._allocation = {}
        # Epochs run on each worker_id, for all current incomplete applications.
        self._steps_run_so_far = {}
        # Time run so far on each worker_id, for all current incomplete
        # applications.
        self._time_run_so_far = {}
        # Cumulative time run so far on each worker_id.
        self._cumulative_time_run_so_far = {}
        # Number of jobs to compute fair share.
        self._num_jobs = 0
        # Commands to run for all current incomplete applications.
        self._jobs = {}
        # Priorities for each application/job and worker_type.
        self._priorities = {}
        # The number of steps to run of each job on each worker type
        # for each iteration.
        self._num_steps_per_iteration = {}
        # Number of failures per job.
        self._num_failures_per_job = {}
        # Throughputs for all job types (pre-measured).
        if throughputs_file is not None:
            self._all_throughputs = utils.read_all_throughputs_json(
                throughputs_file)
        else:
            self._all_throughputs = {}
        # Verbose flag.
        self._verbose = False

        port = SCHEDULER_PORT
        callbacks = {
            'RegisterWorker': self._register_worker_callback,
            'Done': self._done_callback,
        }

        if not self._emulate:
            self.server_thread = threading.Thread(
                target=scheduler_server.serve,
                args=(port, callbacks))
            self.server_thread.daemon = True
            self.server_thread.start()

            self.start_scheduling_thread()


    def start_scheduling_thread(self):
        self.scheduler_thread = threading.Thread(
            target=self.schedule,
            args=())
        self.scheduler_thread.daemon = True
        self.scheduler_thread.start()


    def _update_throughput(self, job_id, worker_type, num_steps,
                           execution_time):
        # Adjust the job throughput using an exponential moving average
        # between the old value and the new measurement.
        old_throughput = self._throughputs[job_id][worker_type]
        new_throughput = num_steps / execution_time
        if old_throughput != INFINITY:
            new_throughput *= EMA_ALPHA
            new_throughput += (1 - EMA_ALPHA) * old_throughput
        self._throughputs[job_id][worker_type] = new_throughput
        print(('[DEBUG] Job %s throughput on worker type %s: '
               '%.3f -> %.3f') % (job_id, worker_type, old_throughput,
                                  self._throughputs[job_id][worker_type]))

    """
    ======================================================================
       Public-facing scheduler methods.
    ======================================================================
    """

    def add_job(self, job, timestamp=None):
        """Adds a new job to the scheduler.

        Enables users to schedule a new job. Updates the internal
        allocation of workers to jobs. An allocation is of the form
        {job: <fraction of allocations on different workers>}.

        Args:
            command: The command to execute.
            total_steps: The total number of steps to run the command for.

        Returns:
            The job_id of the newly added job.
        """

        with self._scheduler_lock:
            job_id = job_id_pair.JobIdPair(self._job_id_counter, None)
            self._job_id_counter += 1
            job._job_id = job_id
            self._jobs[job_id] = job
            self._steps_run_so_far[job_id] = {}
            self._time_run_so_far[job_id] = {}
            self._cumulative_time_run_so_far[job_id] = {}
            self._throughputs[job_id] = {}
            self._num_steps_per_iteration[job_id] = {}
            self._num_failures_per_job[job_id] = 0
            for worker_type in self._worker_types:
                self._steps_run_so_far[job_id][worker_type] = 0
                self._throughputs[job_id][worker_type] = \
                    self._compute_throughput(job.job_type, worker_type)
                if self._job_packing:
                    self._populate_job_combination_metadata(job_id,
                                                            worker_type)
                self._initialize_num_steps_per_iteration(job_id, worker_type)
                self._cumulative_time_run_so_far[job_id][worker_type] = 0.0

            self._reset_time_run_so_far()
            self._add_to_priorities(job_id)
            self._allocation = self._get_allocation()
            current_timestamp = self._get_current_timestamp()
            self._per_job_start_timestamps[job_id] = current_timestamp
            print('%s]\t[Job dispatched]\tJob ID: %s' % (current_timestamp,
                                                         job_id))
        return job_id

    def remove_job(self, job_id):
        """Removes a job from the scheduler.

        Enables users to remove a previously scheduled job. Updates
        the internal allocation of workers to jobs.

        Args:
            job_id: The job_id of the job to remove.
        """

        job_id = job_id_pair.JobIdPair(job_id, None)
        with self._scheduler_lock:
            duration = self._per_job_latest_timestamps[job_id] - \
                self._per_job_start_timestamps[job_id]
            self._job_completion_times[job_id] = duration
            print("Job %d completed\n\tStart timestamp: %.2f\n\t"
                  "End timestamp: %.2f\nDuration: %.2f %s\n" % (
                      job_id[0],
                      self._per_job_start_timestamps[job_id],
                      self._per_job_latest_timestamps[job_id],
                      duration, "seconds")
                  )

            self._reset_time_run_so_far()

            del self._jobs[job_id]
            del self._steps_run_so_far[job_id]
            del self._time_run_so_far[job_id]
            del self._cumulative_time_run_so_far[job_id]
            del self._throughputs[job_id]
            del self._num_failures_per_job[job_id]
            if self._job_packing:
                to_delete = []
                for other_job_id in self._throughputs:
                    if (other_job_id.is_pair() and
                        job_id.overlaps_with(other_job_id)):
                        for only_other_job_id in other_job_id.singletons():
                            if only_other_job_id != job_id:
                                for worker_type in self._worker_types:
                                    self._steps_run_so_far[only_other_job_id][worker_type] += \
                                            self._steps_run_so_far[other_job_id][worker_type]
                        to_delete.append(other_job_id)
                for other_job_id in to_delete:
                    del self._throughputs[other_job_id]
                    del self._steps_run_so_far[other_job_id]
                    del self._time_run_so_far[other_job_id]
                    del self._cumulative_time_run_so_far[other_job_id]

            self._remove_from_priorities(job_id)
            if len(self._throughputs) > 0:
                self._allocation = self._get_allocation()

    def num_workers(self):
        """Returns the number of workers the scheduler is connected to."""

        n = 0
        with self._scheduler_lock:
            for worker_type in self._cluster_spec:
                n += self._cluster_spec[worker_type]
            return n

    def is_done(self):
        """Returns whether the scheduler is done with all its assigned work."""
        with self._scheduler_lock:
            return len(self._jobs) == 0

    def shutdown(self):
        """Sends a shutdown signal to every worker and ends the scheduler."""
        with self._scheduler_lock:
            if len(self._job_completion_times) == 0:
                return
            print('Job completion times:')
            job_ids = sorted([job_id for job_id in self._job_completion_times])
            for job_id in job_ids:
                print('Job %s: %.3f' % (job_id,
                                        self._job_completion_times[job_id]))
            average_job_completion_time = \
                sum([x for x in self._job_completion_times.values()]) / \
                len(self._job_completion_times)
            print('Average job completion time: '
                  '%.3f seconds' % (average_job_completion_time))
            for worker_id in self._worker_connections:
                self._worker_connections[worker_id].shutdown()
        # TODO: Any other cleanup?
        sys.exit(0)

    """
    ======================================================================
       Scheduler's main schedule() and emulate() methods, along with
       associated helper methods.
    ======================================================================
    """

    @preconditions(lambda self: self._emulate or self._scheduler_lock.locked())
    def _schedule_jobs_on_workers_helper(self, worker_type,
                                         already_scheduled_jobs):
        """Solves a Knapsack-like DP problem to determine which applications /
           jobs should run on the specified worker_type in the upcoming round.

           Returns:
             A list of job IDs to schedule on the passed-in worker_type in
             the upcoming round.
        """

        # Only iterate through job_ids that haven't been scheduled yet.
        job_ids = []
        # TODO: Handle job packing.
        for job_id in self._jobs.keys():
            if job_id not in already_scheduled_jobs:
                job_ids.append(job_id)

        num_workers = len(self._worker_type_to_worker_id_mapping[worker_type])

        # DP table initialization.
        A = []
        parent_pointers = {}
        for i in range(len(job_ids)):
            job = self._jobs[job_ids[0]]
            job_id = job.job_id
            scale_factor = job.scale_factor
            A.append([])
            for j in range(num_workers):
                if (i == 0) and ((j+1) >= scale_factor):
                    A[-1].append(self._priorities[worker_type][job_id])
                else:
                    A[-1].append(0.0)

        # Solve Knapsack-like DP problem to determine which applications to
        # run on available workers of the passed-in worker_type.
        if len(A) == 0:
            return []

        for j in range(len(A[0])):
            for i in range(len(A)):
                job = self._jobs[job_ids[i]]
                job_id = job.job_id
                scale_factor = job.scale_factor
                parent_pointer = None

                # If application i is not in the optimal subset of applications
                # to run on workers of type `worker_type`.
                if i > 0 and A[i-1][j] >= A[i][j]:
                    A[i][j] = A[i-1][j]
                    parent_pointer = (i-1, j)

                # If the optimal subset of applications to run on workers of
                # type `worker_type` need only `j-1` GPUs instead of `j`.
                if j > 0 and A[i][j-1] >= A[i][j]:
                    A[i][j] = A[i][j-1]
                    parent_pointer = (i, j-1)

                # If application `i` is in the optimal subset of applications
                # to run on <= j GPUs of type `worker_type`.
                if (i > 0) and (j >= scale_factor):
                    new_priority_sum = (A[i-1][j-scale_factor] +
                        self._priorities[worker_type][job_id])
                    if new_priority_sum > A[i][j]:
                        A[i][j] = new_priority_sum
                        parent_pointer = (i-1, j-scale_factor)

                parent_pointers[(i, j)] = parent_pointer

        # Now route through parent_pointers backward to get the applications
        # that are active in this round.
        (i, j) = (len(job_ids)-1, num_workers-1)
        scheduled_jobs_on_worker_type = []
        while (i, j) in parent_pointers:
            if parent_pointers[(i, j)] is None:
                break
            (i_prime, j_prime) = parent_pointers[(i, j)]
            if (i_prime < i) and (j_prime < j):
                scheduled_jobs_on_worker_type.append((job_ids[i], j-j_prime))
            (i, j) = (i_prime, j_prime)
        scheduled_jobs_on_worker_type.append((job_ids[i], j+1))

        return scheduled_jobs_on_worker_type

    @preconditions(lambda self: self._emulate or self._scheduler_lock.locked())
    def _schedule_jobs_on_workers(self):
        """Attempts to schedule jobs on as many alive workers as possible.

           Returns:
             A list of job IDs and tuple of worker IDs for each scheduled job
             in the coming round.
        """
        # TODO: See if any code needs to be borrowed from _schedule_job_on_worker
        # from master.

        # Update priorities before trying to figure out applications to run
        # in the upcoming round.
        self._update_priorities()

        already_scheduled_jobs = []
        scheduled_jobs = []
        # TODO: Sort self._worker_types in some way for this.
        for worker_type in ["v100", "p100", "k80"]:
            worker_ids = self._worker_type_to_worker_id_mapping[worker_type]
            worker_id_ptr = 0
            scheduled_jobs_on_worker_type = \
                self._schedule_jobs_on_workers_helper(worker_type,
                                                      already_scheduled_jobs)
            for (job_id, scale_factor) in scheduled_jobs_on_worker_type:
                # Make sure a job is only scheduled on a single worker_type in
                # a given round.
                already_scheduled_jobs.append(job_id)

                # For now, ignore locality. Place job_id on the first
                # `scale_factor` workers of the desired type.
                assert(scale_factor == self._jobs[job_id].scale_factor)
                worker_id_ptrs = [worker_id_ptr + i for i in range(scale_factor)]
                scheduled_jobs.append((job_id,
                                       tuple([worker_ids[i] for i in worker_id_ptrs])))
                worker_id_ptr += scale_factor

                for single_job_id in job_id.singletons():
                    num_steps = \
                        self._num_steps_per_iteration[single_job_id][worker_type]
                    self._num_steps_per_iteration[single_job_id][worker_type] = \
                        min(num_steps,
                            self._get_remaining_steps(single_job_id))
                    num_steps = \
                        self._num_steps_per_iteration[single_job_id][worker_type]
                    if num_steps <= 0:
                        raise ValueError('Num steps should be greater'
                                         'than 0, is %d' % (num_steps))
                    worker_types = []
                    for x in self._allocation[single_job_id]:
                        worker_types.append(x)
                    worker_types = sorted(worker_types)
                    allocation_str = ''
                    for x in worker_types:
                        allocation_str += \
                            ' [%4s %.3f]' % (x,
                                             self._allocation[single_job_id][x])
                    print(('%s]\t[Micro-task scheduled]\tJob ID: %s\t'
                           'Worker type: %s\tWorker IDs: %s\t'
                           'Allocation:%s') % (self._get_current_timestamp(),
                                               single_job_id, worker_type,
                                               tuple([worker_ids[i] for i in worker_id_ptrs]),
                                               allocation_str))
                    self._per_job_latest_timestamps[single_job_id] = \
                        self._get_current_timestamp()

        return scheduled_jobs

    def emulate(self, cluster_spec, arrival_times, jobs):
        """Emulates the scheduler execution.

           Using the throughput estimates, this function emulates the
           behavior of a set of jobs submitted to the actual scheduler
           given a cluster specification.

           Currently, the cluster specification must be statically
           specified from the beginning of execution.

           Args:
            cluster_spec: A dictionary of worker type to worker count.
            arrival_times: A list of job arrival times.
            jobs: A dictionary from job ID to Job.
        """

        remaining_jobs = len(jobs)
        queued_jobs = []
        running_jobs = []

        # Set up the cluster according to the provided spec.
        worker_types = sorted([worker_type for worker_type in cluster_spec])
        for worker_type in worker_types:
            for i in range(cluster_spec[worker_type]):
                self._register_worker_callback(worker_type)

        # Add all jobs to the queue.
        for i in range(1, len(arrival_times)):
            assert(arrival_times[i] >= arrival_times[i-1])

        for (arrival_time, job) in zip(arrival_times, jobs):
            queued_jobs.append((arrival_time, job))

        while remaining_jobs > 0:
            # Jump to the next event's timestamp.
            max_timestamp = 0
            if (len(running_jobs) > 0) and (-running_jobs[0][0] > max_timestamp):
                max_timestamp = -running_jobs[0][0]
            if max_timestamp > 0:
                self._current_timestamp = max_timestamp
            else:
                self._current_timestamp = queued_jobs[0][0]

            # Check if any jobs have completed.
            while len(running_jobs) > 0:
                (finish_time, job_id, worker_ids, num_steps) = running_jobs[0]
                finish_time = (-finish_time)
                if finish_time <= self._current_timestamp:
                    for worker_id in worker_ids:
                        self._done_callback(job_id, worker_id, num_steps)
                    if job_id not in self._jobs:
                        remaining_jobs -= 1
                    heapq.heappop(running_jobs)
                else:
                    break

            # Since we're scheduling in rounds, no jobs should be running when
            # scheduling the next round of jobs.
            assert(len(running_jobs) == 0)

            # Dispatch any newly arrived jobs.
            while len(queued_jobs) > 0:
                (arrival_time, job) = queued_jobs[0]
                if arrival_time <= self._current_timestamp:
                    job_id = self.add_job(job)
                    queued_jobs.pop(0)
                else:
                    break

            # Schedule jobs onto available worker_ids for next round.
            scheduled_jobs = self._schedule_jobs_on_workers()
            for (job_id, worker_ids) in scheduled_jobs:
                worker_type = self._worker_id_to_worker_type_mapping[worker_ids[0]]
                num_steps = self._num_steps_per_iteration[job_id][worker_type]
                for worker_id in worker_ids:
                    self._remove_available_worker_id(worker_id)
                finish_time = (self._current_timestamp +
                                (num_steps /
                                   self._throughputs[job_id][worker_type]))
                heapq.heappush(running_jobs, (-finish_time, job_id, worker_ids, num_steps))

        print('Total duration: %.3f seconds' % (self._current_timestamp))

    def schedule(self):
        """Schedules jobs on workers.

        In a loop, schedules the inactive application most in need of an
        available worker (that is, the worker with the lowest
        fraction_run/fraction_allocated ratio).

        Scheduler holds two internal data structures,
        {(application, worker_type): time_run_on_worker}
        & {(application, worker_type): allocation_fraction}.
        As an algorithmic optimization, the scheduler maintains
        a heap of all currently inactive applications for each
        worker, sorted by fraction_run/fraction_allocated ratio.
        """

        while True:
            with self._scheduler_lock:
                num_workers = len(self.worker_ids)
                # Reset available_worker_ids to the desired size.
                self._available_worker_ids = queue.Queue(self.num_workers)
                for worker_id in self.worker_ids:
                    self._add_available_worker_id(worker_id)
                scheduled_jobs = self._schedule_jobs_on_workers()
                for (job_id, worker_ids) in scheduled_jobs:
                    worker_type = self._worker_id_to_worker_type_mapping[worker_ids[0]]
                    num_steps = self._num_steps_per_iteration[job_id][worker_type]
                    for worker_id in worker_ids:
                        self._worker_connections[worker_id].run(
                            [(job_id[0], self._jobs[job_id].command,
                              self._jobs[job_id].num_steps_arg,
                              num_steps)])
                        self._remove_available_worker_id(worker_id)
            self._wait_until_all_workers_available(num_workers)

    @preconditions(lambda self: self._emulate or self._scheduler_lock.locked())
    def _get_allocation(self):
        """Computes the allocation.

        Uses the specified policy to compute an allocation of jobs to
        compute resources. Requires self._scheduler_lock to be held
        when calling this function.

        Returns:
            A 2-level dict indexed by job_id and then worker_type. For
            example,

            {0: {"v100": 0.25, "p100": 0.95}, 1: {"v100": 0.75, "p100": 0.05}}

            indicates that for 25% of the time, worker type 'v100' should run,
            job 0 and for 95% of the time, worker type 'p100' should run job 0.
        """

        unflattened_allocation = self._policy.get_allocation(
            self._throughputs, self._cluster_spec)
        return unflattened_allocation

    @preconditions(lambda self: self._emulate or self._scheduler_lock.locked())
    def _populate_job_combination_metadata(self, job_id, worker_type):
        """Populate metadata for job combinations involving passed-in job_id."""

        job = self._jobs[job_id]
        for other_job_id in self._jobs:
            if other_job_id != job_id:
                other_job = self._jobs[other_job_id]
                merged_job_id = \
                        self.JobIdPair(job_id[0], other_job_id[0])
                if merged_job_id not in self._throughputs:
                    self._throughputs[merged_job_id] = {}
                    self._steps_run_so_far[merged_job_id] = {}
                    self._time_run_so_far[merged_job_id] = {}
                    self._cumulative_time_run_so_far[merged_job_id] = {}
                self._throughputs[merged_job_id][worker_type] = \
                    self._compute_throughput([job, other_job], worker_type)
                self._steps_run_so_far[merged_job_id][worker_type] = 0

    def _compute_throughput(self, job_type, worker_type):
        if self._emulate:
            return self._all_throughputs[job_type][worker_type]
        else:
            return DEFAULT_THROUGHPUT

    @preconditions(lambda self: self._emulate or self._scheduler_lock.locked())
    def _reset_time_run_so_far(self):
        """Reset _time_run_so_far so that all jobs receive new fair allocation
        from here on out.

        Requires self._scheduler_lock to be held when calling this function.
        """
        for worker_type in self._worker_types:
            for job_id in self._time_run_so_far:
                self._time_run_so_far[job_id][worker_type] = 0.0

    @preconditions(lambda self: self._emulate or self._scheduler_lock.locked())
    def _add_to_priorities(self, job_id, worker_type=None):
        """Adds a job_id to each worker type's priority list.

        Requires self._scheduler_lock to be held when calling this function.

        Args:
            job_id: The job_id to add to the workers' queues.
        """

        worker_types = self._worker_types
        if worker_type is not None:
            worker_types = [worker_type]
        for worker_type in worker_types:
            self._priorities[worker_type][job_id] = 0.0
            for other_job_id in self._throughputs:
                if (other_job_id.is_pair() and
                    job_id.overlaps_with(other_job_id)):
                    self._priorities[worker_type][other_job_id] = 0.0

    @preconditions(lambda self: self._emulate or self._scheduler_lock.locked())
    def _remove_from_priorities(self, job_id):
        """Removes a job_id from each worker type's priority list.

        Requires self._scheduler_lock to be held when calling this function.

        Args:
           job_id: The job_id to remove from the workers' queues.
        """
        for worker_type in self._worker_types:
            while True:
                found = False
                for other_job_id in self._priorities[worker_type]:
                    if job_id.overlaps_with(other_job_id):
                        del self._priorities[worker_type][other_job_id]
                        found = True
                        break
                if not found:
                    break

    @preconditions(lambda self: self._emulate or self._scheduler_lock.locked())
    def _update_priorities(self):
        """Updates each per-worker queue.

        Re-sorts the queue of each worker to compute the next job to run.
        For a given worker w_i, the next job to be scheduled will be the job
        that has so far received the smallest fraction of its computed
        fair allocation.
        Requires self._scheduler_lock to be held when calling this function.

        Args:
            job_id: The job_id to add to the workers' queues.
        """

        # Stores the fraction of time spent running a job for each worker.
        fractions = {}

        # Stores the total amount of time run on each worker among currently
        # running jobs.
        tot_time_run = {}

        for worker_type in self._worker_types:
            fractions[worker_type] = {}
            tot_time_run[worker_type] = self._get_total_time_run(worker_type)

        for worker_type in self._worker_types:
            for job_id in self._time_run_so_far:
                if tot_time_run[worker_type] == 0.0:
                    fraction = 1.0 / len(self._worker_types)
                else:
                    fraction = self._time_run_so_far[job_id][worker_type] / \
                        tot_time_run[worker_type]
                fractions[worker_type][job_id] = fraction
            for job_id in self._priorities[worker_type]:
                new_priority = float("inf")
                if self._allocation[job_id][worker_type] == 0.0:
                    new_priority = 0.0
                elif fractions[worker_type][job_id] > 0.0:
                    new_priority = self._allocation[job_id][worker_type] /\
                            fractions[worker_type][job_id]
                self._priorities[worker_type][job_id] = new_priority

    def _add_available_worker_id(self, worker_id):
        """Adds a worker_id to the list of available workers."""

        self._available_worker_ids.put(worker_id)

    def _remove_available_worker_id(self, worker_id=None):
        """Returns the worker_id of the next available worker."""

        if self._emulate:
            try:
                return self._available_worker_ids.get_nowait()
            except queue.Empty as e:
                return None
        else:
            return self._available_worker_ids.get()

    def _wait_until_all_worker_ids_available(self, num_worker_ids):
        """Wait until all worker_ids available."""
        while not self._available_worker_ids.full():
            time.sleep(1)

    @preconditions(lambda self: self._emulate or self._scheduler_lock.locked())
    def _get_total_steps_run(self, job_id):
        """Returns the total number of steps run for job with id job_id."""

        # TODO: change to exception
        assert(job_id in self._steps_run_so_far)
        total_steps_run = 0
        for worker_type in self._steps_run_so_far[job_id]:
            total_steps_run += self._steps_run_so_far[job_id][worker_type]
        for other_job_id in self._steps_run_so_far:
            if other_job_id.is_pair() and job_id.overlaps_with(other_job_id):
                for worker_type in self._steps_run_so_far[other_job_id]:
                    total_steps_run += \
                            self._steps_run_so_far[other_job_id][worker_type]
        return total_steps_run

    @preconditions(lambda self: self._emulate or self._scheduler_lock.locked())
    def _get_remaining_steps(self, job_id):
        steps_run_so_far = self._get_total_steps_run(job_id)
        return self._jobs[job_id].total_steps - steps_run_so_far

    @preconditions(lambda self: self._emulate or self._scheduler_lock.locked())
    def _get_total_time_run(self, worker_type):
        """Returns the total time run on worker_type since the last reset."""

        total_time_run = 0.0
        for job_id in self._time_run_so_far:
            if worker_type in self._time_run_so_far[job_id]:
                total_time_run += self._time_run_so_far[job_id][worker_type]
        return total_time_run

    @preconditions(lambda self: self._emulate or self._scheduler_lock.locked())
    def _get_current_timestamp(self):
        if self._emulate:
            return self._current_timestamp
        else:
            return time.time()

    @preconditions(lambda self: self._emulate or self._scheduler_lock.locked())
    def _initialize_num_steps_per_iteration(self, job_id, worker_type):
        if self._policy.name == 'FIFO':
            num_steps = self._jobs[job_id].total_steps
        elif self._emulate:
            throughput = self._throughputs[job_id][worker_type]
            num_steps = int(throughput * TIME_PER_ITERATION)
        else:
            num_steps = min(DEFAULT_NUM_STEPS,
                            self._get_remaining_steps(job_id))
        self._num_steps_per_iteration[job_id][worker_type] = num_steps

    @preconditions(lambda self: self._emulate or self._scheduler_lock.locked())
    def _update_num_steps_per_iteration(self, job_id, worker_type,
                                        num_steps, execution_time):
        # Adjust the number of steps in each iteration such that the
        # new number of steps will more closely align with the
        # desired wall-clock time per iteration.
        # TODO(keshav2): Use num_steps instead?
        old_num_steps_per_iteration = \
                self._num_steps_per_iteration[job_id][worker_type]
        new_num_steps_per_iteration = old_num_steps_per_iteration * \
                TIME_PER_ITERATION / execution_time
        new_num_steps_per_iteration = \
                max(1, new_num_steps_per_iteration)
        self._num_steps_per_iteration[job_id][worker_type] = \
                int(EMA_ALPHA * new_num_steps_per_iteration +
                        (1 - EMA_ALPHA) * old_num_steps_per_iteration)
        self._num_steps_per_iteration[job_id][worker_type] = \
                min(self._get_remaining_steps(job_id),
                    self._num_steps_per_iteration[job_id][worker_type])
        print(('[DEBUG] Job %s steps per iteration on worker type %s: '
               '%d -> %d') % (job_id, worker_type,
                              old_num_steps_per_iteration,
                              self._num_steps_per_iteration[job_id][worker_type]))

    """
    ======================================================================
       Callback methods called by workers.
    ======================================================================
    """

    def _register_worker_callback(self, worker_type, ip_addr=None, port=None):
        """Registers a worker with the scheduler.

        Initializes state for a new worker and assigns it an id.
        The worker provides an IP address and port for its RPC server
        so that the scheduler can establish an RPC client for
        scheduler-to-worker communication. The worker also
        enumerates its available devices so that the scheduler
        can make fine-grained scheduling decisions.

        Args:
            ip_addr: IP address of the worker's RPC server.
            port: Port number for the worker's RPC server.
            devices: List of available devices on the worker.

        Returns:
            The worker_id of the newly registered worker.
        """

        with self._scheduler_lock:
            worker_id = self._worker_id_counter
            self._worker_ids.append(worker_id)
            self._worker_id_counter += 1
            self._worker_types.add(worker_type)
            self._worker_id_to_worker_type_mapping[worker_id] = worker_type
            if worker_type not in self._worker_type_to_worker_id_mapping:
                self._worker_type_to_worker_id_mapping[worker_type] = []
            self._worker_type_to_worker_id_mapping[worker_type].append(worker_id)

            if worker_type not in self._priorities:
                self._priorities[worker_type] = {}
                for job_id in self._jobs:
                    self._steps_run_so_far[job_id][worker_type] = 0
                    self._time_run_so_far[job_id][worker_type] = 0
                    self._cumulative_time_run_so_far[job_id][worker_type] = 0.0
                    self._throughputs[job_id][worker_type] = \
                        self._compute_throughput(self._jobs[job_id],
                                                 worker_type)
                    if self._job_packing:
                        self._populate_job_combination_metadata(job_id,
                                                                worker_type)

                    self._initialize_num_steps_per_iteration(job_id, worker_type)
                    self._add_to_priorities(job_id, worker_type=worker_type)

                self._reset_time_run_so_far()

            if worker_type not in self._cluster_spec:
                self._cluster_spec[worker_type] = 0
            self._cluster_spec[worker_type] += 1
            if not self._emulate:
                self._worker_connections[worker_id] = \
                    scheduler_client.SchedulerRpcClient(ip_addr, port)

            self._allocation = self._get_allocation()

        return worker_id

    def _done_callback(self, job_id, worker_id, num_steps, execution_time=None):
        """Handles completion of a scheduled job.

        Updates the running total of completed steps and time spent on each
        worker, for every currently active application. Removes the job from
        the scheduler if the job has finished all its requested steps. Adds
        the worker back to the list of available workers.

        Args:
            job_id: The id of the completed job.
            worker_id: The id of the worker where the job was completed.
            num_steps: The number of steps the job ran for.
        """

        to_remove = []
        with self._scheduler_lock:
            worker_type = self._worker_id_to_worker_type_mapping[worker_id]
            current_timestamp = self._get_current_timestamp()
            if self._emulate:
                # TODO: Fix this.
                for single_job_id in job_id.singletons():
                    start_timestamp = self._per_job_latest_timestamps[single_job_id]
                execution_time = current_timestamp - start_timestamp
            if execution_time < 0:
                # Job failed.
                self._num_failures_per_job[job_id] += 1
                print(('%s]\t[Micro-task failed]\t'
                       'Job ID: %s') % (current_timestamp,
                                        job_id))
                if self._num_failures_per_job[job_id] >= MAX_FAILED_ATTEMPTS:
                    print(('%s]\t[Job failed]\t'
                           'Job ID: %s') % (current_timestamp, job_id))
                    to_remove.append(job_id)
                self._add_available_worker_id(worker_id)
            else:
                self._num_failures_per_job[job_id] = 0
                self._steps_run_so_far[job_id][worker_type] += num_steps
                self._time_run_so_far[job_id][worker_type] += execution_time
                self._cumulative_time_run_so_far[job_id][worker_type] += \
                        execution_time

                if not self._emulate:
                    self._update_num_steps_per_iteration(job_id, worker_type,
                                                         num_steps,
                                                         execution_time)
                    old_throughput = self._throughputs[job_id][worker_type]
                    self._update_throughput(job_id, worker_type, num_steps,
                                            execution_time)


                for single_job_id in job_id.singletons():
                    self._per_job_latest_timestamps[single_job_id] = \
                            self._get_current_timestamp()
                print(('%s]\t[Micro-task succeeded]\t'
                       'Job ID: %s\tWorker type: %s\t'
                       'Worker ID: %d') % (current_timestamp,
                                           job_id,
                                           worker_type,
                                           worker_id))

                for single_job_id in job_id.singletons():
                    if not (self._get_total_steps_run(single_job_id) <
                        self._jobs[single_job_id].total_steps):
                        print(('%s]\t[Job succeeded]\t'
                               'Job ID: %s') % (current_timestamp,
                                                single_job_id))
                        to_remove.append(single_job_id)

                self._add_available_worker_id(worker_id)

        for single_job_id in to_remove:
            self.remove_job(single_job_id[0])
