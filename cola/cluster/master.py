#!/usr/bin/env python
# -*- coding: utf-8 -*-
'''
Copyright (c) 2013 Qin Xuye <qin@qinxuye.me>

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

Created on 2014-6-12

@author: chine
'''

import os
import time
import threading

from cola.functions.counter import CounterServer
from cola.functions.budget import BudgetApplyServer, ALLFINISHED
from cola.functions.speed import SpeedControlServer
from cola.cluster.tracker import WorkerTracker, JobTracker
from cola.cluster.stage import Stage
from cola.core.rpc import FileTransportServer, FileTransportClient, \
                            client_call
from cola.core.zip import ZipHandler
from cola.core.utils import import_job_desc

RUNNING, HANGUP, STOPPED = range(3)
CONTINOUS_HEARTBEAT = 90
HEARTBEAT_INTERVAL = 20
HEARTBEAT_CHECK_INTERVAL = 3*HEARTBEAT_INTERVAL
JOB_CHECK_INTERVAL = 5

class JobMaster(object):
    def __init__(self, ctx, job_name, job_desc):
        self.working_dir = os.path.join(ctx.working_dir, 'master', 
                                        'tracker', job_name)
        if not os.path.exists(self.working_dir):
            os.makedirs(self.working_dir)
            
        self.job_name = job_name
        self.job_desc = job_desc
        self.settings = job_desc.settings
        self.rpc_server = ctx.master_rpc_server
        
        self.stopped = threading.Event()
        
        self.inited = False
        self.init()
        
        self.workers = ctx.addrs[:]
            
    def _init_counter_server(self):
        counter_dir = os.path.join(self.working_dir, 'counter')
        self.counter_server = CounterServer(counter_dir, self.settings,
                                            rpc_server=self.rpc_server, 
                                            app_name=self.job_name)
        
    def _init_budget_server(self):
        budget_dir = os.path.join(self.working_dir, 'budget')
        self.budget_server = BudgetApplyServer(budget_dir, self.settings,
                                          rpc_server=self.rpc_server, 
                                          app_name=self.job_name)
        
    def _init_speed_server(self):
        speed_dir = os.path.join(self.working_dir, 'speed')
        self.speed_server = SpeedControlServer(speed_dir, self.settings,
                                               rpc_server=self.rpc_server,
                                               app_name=self.job_name)
        
    def init(self):
        if self.inited:
            return
        
        self._init_counter_server()
        self._init_budget_server()
        self._init_speed_server()
        
        self.inited = True
        
    def remove_worker(self, worker):
        if worker not in self.workers:
            return
        
        # rpc call the other workers to remove this worker
        self.workers.remove(worker)
        for node in self.workers:
            client_call(node, 'remove_node', worker)
        
    def add_worker(self, worker):
        if worker in self.workers:
            return
        
        # rpc call the other workers to add this worker
        for node in self.workers:
            client_call(node, 'add_node', worker)
        self.workers.append(worker)
        
    def has_worker(self, worker):
        return worker in self.workers
    
    def shutdown(self):
        if not self.inited:
            return
        
        self.counter_server.shutdown()
        self.budget_server.shutdown()
        self.speed_server.shutdown()
        
        self.inited = False

class Master(object):
    def __init__(self, ctx):
        self.ctx = ctx
        self.rpc_server = self.ctx.master_rpc_server
        assert self.rpc_server is not None
        
        self.working_dir = os.path.join(self.ctx.working_dir, 'master')
        self.zip_dir = os.path.join(self.working_dir, 'zip')
        self.job_dir = os.path.join(self.working_dir, 'jobs')
        
        self.worker_tracker = WorkerTracker()
        self.job_tracker = JobTracker()
        
        self.stopped = threading.Event()
        
        self._register_rpc()
        
        FileTransportServer(self.rpc_server, self.zip_dir)
        
    def _register_rpc(self):
        self.rpc_server.register_function(self.run_job, 'run_job')
        self.rpc_server.register_function(self.stop_job, 'stop_job')
        self.rpc_server.register_function(self.shutdown, 'shutdown')
        self.rpc_server.register_function(self.register_heartbeat, 
                                          'register_heartbeat')
        
    def register_heartbeat(self, worker):
        self.worker_tracker.register_worker(worker)
        return self.worker_tracker.workers.keys()
    
    def _check_workers(self):
        while not self.stopped.is_set():
            for worker, info in self.worker_tracker.workers.iteritems():
                # if loose connection
                if int(time.time()) - info.last_update \
                    > HEARTBEAT_CHECK_INTERVAL:
                    
                    info.continous_register = 0
                    if info.status == RUNNING:
                        info.status = HANGUP
                    elif info.status == HANGUP:
                        info.status = STOPPED
                        self.black_list.append(worker)
                        
                        for job in self.job_tracker.running_jobs:
                            self.job_tracker.remove_worker(job, worker)
                        
                # if continously connect for more than 10 min
                elif info.continous_register >= CONTINOUS_HEARTBEAT:
                    if info.status != RUNNING:
                        info.status = RUNNING
                    if worker in self.black_list:
                        self.black_list.remove(worker)
                        
                    for job in self.job_tracker.running_jobs:
                        if not client_call(worker, 'has_job'):
                            client_call(worker, 'prepare', job)
                            client_call(worker, 'run_job', job)
                        self.job_tracker.add_worker(job, worker)
                
            self.stopped.wait(HEARTBEAT_CHECK_INTERVAL)
                        
    def _check_jobs(self):
        while not self.stopped.is_set():
            for job_master in self.job_tracker.running_jobs.values():
                if job_master.budget_server.get_status() == ALLFINISHED:
                    self.stop_job(job_master.job_name)
                    self.job_tracker.remove_job(job_master.job_name)
            self.stopped.wait(JOB_CHECK_INTERVAL)
                        
    def _unzip(self, job_name):
        zip_file = os.path.join(self.zip_dir, job_name)
        if os.path.exists(zip_file):
            ZipHandler.uncompress(zip_file, self.job_dir)
                        
    def run(self):
        self._worker_t = threading.Thread(target=self._check_workers)
        self._worker_t.start()
        
        self._job_t = threading.Thread(target=self._check_jobs)
        self._job_t.start()
        
    def run_job(self, job_name, unzip=False):
        if unzip:
            self._unzip(job_name)
        
        job_path = os.path.join(self.job_dir, job_name)
        job_desc = import_job_desc(job_path)
        job_master = JobMaster(self.ctx, job_name, job_desc)
        job_master.init()
        self.job_tracker.register_job(job_name, job_master)
        
        zip_file = os.path.join(self.zip_dir, job_name)
        for worker in self.ctx.workers:
            FileTransportClient(worker, zip_file).send_file()
        
        stage = Stage(self.ctx.workers, self.rpc_server, 'prepare')
        stage.barrier(True, job_name)
        
        stage = Stage(self.ctx.workers, self.rpc_server, 'run_job')
        stage.barrier(True, job_name)
        
    def stop_job(self, job_name):
        stage = Stage(self.job_tracker.get_job_master(job_name).workers,
                      self.rpc_server, 'stop_job')
        stage.barrier(True, job_name)
        
        stage = Stage(self.job_tracker.get_job_master(job_name).workers,
                      self.rpc_server, 'clear_job')
        stage.barrier(True, job_name)
        
    def has_running_jobs(self):
        return len(self.job_tracker.running_jobs) > 0
        
    def _stop_all_jobs(self):
        for job_name in self.job_tracker.running_jobs.keys():
            self.stop_job(job_name)
            
    def _shutdown_all_workers(self):
        stage = Stage(self.worker_tracker.workers, self.rpc_server,
                      'shutdown')
        stage.barrier(True)
        
    def shutdown(self):
        if not hasattr(self, '_worker_t'):
            return
        if not hasattr(self, '_job_t'):
            return
        self.stopped.set()
        self._stop_all_jobs()
        self._shutdown_all_workers()
        
        self._worker_t.join()
        self._job_t.join()
        
        self.rpc_server.shutdown()