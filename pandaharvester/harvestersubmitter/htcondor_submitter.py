import os
import tempfile
import subprocess
import random

from concurrent.futures import ThreadPoolExecutor

import re

from pandaharvester.harvesterconfig import harvester_config
from pandaharvester.harvestercore import core_utils
from pandaharvester.harvestercore.plugin_base import PluginBase
from pandaharvester.harvestermisc.info_utils import PandaQueuesDict


# logger
baseLogger = core_utils.setup_logger('htcondor_submitter')


# Integer division round up
def _div_round_up(a, b):
    return a // b + int(a % b > 0)


def _condor_macro_replace(string, **kwarg):
    new_string = string
    macro_map = {
                '\$\(Cluster\)': str(kwarg['ClusterId']),
                '\$\(Process\)': '0',
                }
    for k, v in macro_map.items():
        new_string = re.sub(k, v, new_string)
    return new_string


# submit a worker
def submit_a_worker(data):
    workspec = data['workspec']
    template = data['template']
    log_dir = data['log_dir']
    n_core_per_node = data['n_core_per_node']
    panda_queue_name = data['panda_queue_name']
    x509_user_proxy = data['x509_user_proxy']
    ce_info_dict = data['ce_info_dict']
    batch_log_dict = data['batch_log_dict']
    special_par = data['special_par']
    workspec.reset_changed_list()
    # make logger
    tmpLog = core_utils.make_logger(baseLogger, 'workerID={0}'.format(workspec.workerID),
                                    method_name='submit_a_worker')
    # make batch script
    # batchFile = make_batch_script(workspec=workspec, template=template, n_core_per_node=n_core_per_node, log_dir=log_dir,
    #                                 panda_queue_name=panda_queue_name, x509_user_proxy=x509_user_proxy,
    #                                 ce_info_dict=ce_info_dict, batch_log_dict=batch_log_dict, special_par=special_par)
    batchFile = make_batch_script(**data)
    # command
    comStr = 'condor_submit {0}'.format(batchFile)
    # submit
    tmpLog.debug('submit with {0}'.format(batchFile))
    try:
        p = subprocess.Popen(comStr.split(),
                             shell=False,
                             universal_newlines=True,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        # check return code
        stdOut, stdErr = p.communicate()
        retCode = p.returncode
    except:
        stdOut = ''
        stdErr = core_utils.dump_error_message(tmpLog, no_message=True)
        retCode = 1
    tmpLog.debug('retCode={0}'.format(retCode))
    if retCode == 0:
        # extract batchID
        job_id_match = None
        for tmp_line_str in stdOut.split('\n'):
            job_id_match = re.search('^(\d+) job[(]s[)] submitted to cluster (\d+)\.$', tmp_line_str)
            if job_id_match:
                break
        if job_id_match is not None:
            workspec.batchID = job_id_match.group(2)
            tmpLog.debug('batchID={0}'.format(workspec.batchID))
            batch_log = _condor_macro_replace(batch_log_dict['batch_log'], ClusterId=workspec.batchID)
            batch_stdout = _condor_macro_replace(batch_log_dict['batch_stdout'], ClusterId=workspec.batchID)
            batch_stderr = _condor_macro_replace(batch_log_dict['batch_stderr'], ClusterId=workspec.batchID)
            workspec.set_log_file('batch_log', batch_log)
            workspec.set_log_file('stdout', batch_stdout)
            workspec.set_log_file('stderr', batch_stderr)
            tmpRetVal = (True, '')
        else:
            errStr = 'batchID cannot be found'
            tmpLog.error(errStr)
            tmpRetVal = (False, errStr)
    else:
        # failed
        errStr = '{0} \n {1}'.format(stdOut, stdErr)
        tmpLog.error(errStr)
        tmpRetVal = (False, errStr)
    return tmpRetVal, workspec.get_changed_attributes()


# make batch script
def make_batch_script(workspec, template, n_core_per_node, log_dir, panda_queue_name, x509_user_proxy, ce_info_dict=dict(), batch_log_dict=dict(), special_par=''):
    # make logger
    tmpLog = core_utils.make_logger(baseLogger, 'workerID={0}'.format(workspec.workerID),
                                    method_name='make_batch_script')
    tmpFile = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='_submit.sdf', dir=workspec.get_access_point())

    # Note: In workspec, unit of minRamCount and of maxDiskCount are both MB.
    #       In HTCondor SDF, unit of request_memory is MB, and request_disk is KB.
    n_core_total = workspec.nCore if workspec.nCore else n_core_per_node
    request_ram = max(workspec.minRamCount, 1 * n_core_total) if workspec.minRamCount else 1 * n_core_total
    request_disk = workspec.maxDiskCount * 1024 if workspec.maxDiskCount else 1
    request_walltime = workspec.maxWalltime if workspec.maxWalltime else 0
    ce_info_dict = ce_info_dict.copy()
    batch_log_dict = batch_log_dict.copy()

    # possible override by AGIS special_par
    if special_par:
        special_par_attr_list = ['queue', 'maxWallTime', 'xcount', ]
        _match_special_par_dict = { attr: re.search('\({attr}=([^)]+)\)'.format(attr=attr), special_par) \
                                        for attr in special_par_attr_list }
        for attr, _match in _match_special_par_dict.items():
            if not _match:
                continue
            elif attr == 'queue':
                ce_info_dict['ce_queue_name'] = str(_match.group(1))
            elif attr == 'maxWallTime':
                request_walltime = int(_match.group(1))
            elif attr == 'xcount':
                n_core_total = int(_match.group(1))
            tmpLog.debug('job attributes override by AGIS special_par: {0}={1}'.format(attr, str(_match.group(1))))

    # derived job attributes
    n_node = _div_round_up(n_core_total, n_core_per_node)
    request_ram_per_core = _div_round_up(request_ram * n_node, n_core_total)
    request_cputime = request_walltime * n_core_total
    request_walltime_minute = _div_round_up(request_walltime, 60)
    request_cputime_minute = _div_round_up(request_cputime, 60)

    # fill in template
    tmpFile.write(template.format(
        nCorePerNode=n_core_per_node,
        nCoreTotal=n_core_total,
        nNode=n_node,
        requestRam=request_ram,
        requestRamPerCore=request_ram_per_core,
        requestDisk=request_disk,
        requestWalltime=request_walltime,
        requestWalltimeMinute=request_walltime_minute,
        requestCputime=request_cputime,
        requestCputimeMinute=request_cputime_minute,
        accessPoint=workspec.accessPoint,
        harvesterID=harvester_config.master.harvester_id,
        workerID=workspec.workerID,
        computingSite=workspec.computingSite,
        pandaQueueName=panda_queue_name,
        x509UserProxy=x509_user_proxy,
        ceEndpoint=ce_info_dict.get('ce_endpoint', ''),
        ceHostname=ce_info_dict.get('ce_hostname', ''),
        ceFlavour=ce_info_dict.get('ce_flavour', ''),
        ceJobmanager=ce_info_dict.get('ce_jobmanager', ''),
        ceQueueName=ce_info_dict.get('ce_queue_name', ''),
        ceVersion=ce_info_dict.get('ce_version', ''),
        logDir=log_dir,
        gtag=batch_log_dict.get('gtag', 'fake_GTAG_string'),
        )
    )
    tmpFile.close()
    tmpLog.debug('done')
    return tmpFile.name


# parse log, stdout, stderr filename
def parse_batch_job_filename(value_str, file_dir, batchID, guess=False):
    _filename = os.path.basename(value_str)
    if guess:
        # guess file name before files really created; possibly containing condor macros
        return _filename
    else:
        _sanitized_list = re.sub('\{(\w+)\}|\[(\w+)\]|\((\w+)\)|#(\w+)#|\$', '',  _filename).split('.')
        _prefix = _sanitized_list[0]
        _suffix = _sanitized_list[-1] if len(_sanitized_list) > 1 else ''

        for _f in os.listdir(file_dir):
            if re.match('{prefix}(.*)\.{batchID}\.(.*)\.{suffix}'.format(prefix=_prefix, suffix=_suffix, batchID=batchID), _f):
                return _f
        return None


# submitter for HTCONDOR batch system
class HTCondorSubmitter(PluginBase):
    # constructor
    def __init__(self, **kwarg):
        self.logBaseURL = None
        PluginBase.__init__(self, **kwarg)
        # number of processes
        try:
            self.nProcesses
        except AttributeError:
            self.nProcesses = 1
        else:
            if (not self.nProcesses) or (self.nProcesses < 1):
                self.nProcesses = 1
        # condor log directory
        try:
            self.logDir
        except AttributeError:
            self.logDir = os.getenv('TMPDIR') or '/tmp'
        # x509 proxy
        try:
            self.x509UserProxy
        except AttributeError:
            self.x509UserProxy = os.getenv('X509_USER_PROXY')
        # ATLAS AGIS
        try:
            self.useAtlasAGIS = bool(self.useAtlasAGIS)
        except AttributeError:
            self.useAtlasAGIS = False
        # ATLAS Grid CE, requiring AGIS
        try:
            self.useAtlasGridCE = bool(self.useAtlasGridCE)
        except AttributeError:
            self.useAtlasGridCE = False
        finally:
            self.useAtlasAGIS = self.useAtlasAGIS or self.useAtlasGridCE
        # sdf template directories of CEs
        try:
            self.CEtemplateDir
        except AttributeError:
            self.CEtemplateDir = ''

    # submit workers
    def submit_workers(self, workspec_list):
        tmpLog = core_utils.make_logger(baseLogger, method_name='submit_workers')

        nWorkers = len(workspec_list)
        tmpLog.debug('start nWorkers={0}'.format(nWorkers))

        # get queue info from AGIS by cacher in db
        if self.useAtlasAGIS:
            panda_queues_dict = PandaQueuesDict()
            panda_queue_name = panda_queues_dict.get_PQ_from_PR(self.queueName)
            this_panda_queue_dict = panda_queues_dict.get(self.queueName, dict())
            # tmpLog.debug('panda_queues_name and queue_info: {0}, {1}'.format(self.queueName, panda_queues_dict[self.queueName]))
        else:
            panda_queues_dict = dict()
            panda_queue_name = self.queueName
            this_panda_queue_dict = dict()

        def _handle_one_worker(workspec):
            # make logger
            tmpLog = core_utils.make_logger(baseLogger, 'workerID={0}'.format(workspec.workerID),
                                            method_name='_handle_one_worker')

            # get default information from queue info
            n_core_per_node_from_queue = this_panda_queue_dict.get('corecount', 1) if this_panda_queue_dict.get('corecount', 1) else 1
            ce_info_dict = dict()
            batch_log_dict = dict()
            special_par = ''

            if self.useAtlasGridCE:
                # If ATLAS Grid CE mode used
                tmpLog.debug('Using ATLAS Grid CE mode...')
                queues_from_queue_list = this_panda_queue_dict.get('queues', [])
                special_par = this_panda_queue_dict.get('special_par', '')
                ce_endpoint_from_queue = ''
                ce_flavour_str = ''
                ce_version_str = ''
                random.shuffle(queues_from_queue_list)
                for _queue_dict in queues_from_queue_list:
                    if _queue_dict.get('ce_endpoint') and str(_queue_dict.get('ce_state', '')).upper() == 'ACTIVE':
                        ce_flavour_str = str( _queue_dict.get('ce_flavour', '') ).lower()
                        ce_version_str = str( _queue_dict.get('ce_version', '') ).lower()
                        if ce_flavour_str in set(['arc-ce', 'cream-ce', 'htcondor-ce']):
                            ce_info_dict = _queue_dict.copy()
                            ce_endpoint_from_queue = ce_info_dict.get('ce_endpoint', '')
                            ce_info_dict['ce_hostname'] = re.sub(':\w*', '',  ce_endpoint_from_queue)
                            break
                        else:
                            ce_flavour_str = ''
                tmpLog.debug('For site {0} got CE endpoint: "{1}", flavour: "{2}"'.format(self.queueName, ce_endpoint_from_queue, ce_flavour_str))
                if os.path.isdir(self.CEtemplateDir) and ce_flavour_str:
                    sdf_template_filename = '{ce_flavour_str}.sdf'.format(ce_flavour_str=ce_flavour_str)
                    self.templateFile = os.path.join(self.CEtemplateDir, sdf_template_filename)

            # template for batch script
            tmpFile = open(self.templateFile)
            sdf_template = tmpFile.read()
            tmpFile.close()

            # get batch_log, stdout, stderr filename
            for _line in sdf_template.split('\n'):
                if _line.startswith('#'):
                    continue
                _match_batch_log = re.match('log = (.+)', _line)
                _match_stdout = re.match('output = (.+)', _line)
                _match_stderr = re.match('error = (.+)', _line)
                if _match_batch_log:
                    batch_log_value = _match_batch_log.group(1)
                    continue
                if _match_stdout:
                    stdout_value = _match_stdout.group(1)
                    continue
                if _match_stderr:
                    stderr_value = _match_stderr.group(1)
                    continue

            # get override requirements from queue configured
            try:
                n_core_per_node = self.nCorePerNode if self.nCorePerNode else n_core_per_node_from_queue
            except AttributeError:
                n_core_per_node = n_core_per_node_from_queue

            # URLs for log files
            if not (self.logBaseURL is None):
                if workspec.batchID:
                    batchID = workspec.batchID
                    guess = False
                else:
                    batchID = ''
                    guess = True
                batch_log_filename = parse_batch_job_filename(value_str=batch_log_value, file_dir=self.logDir, batchID=batchID, guess=guess)
                stdout_path_file_name = parse_batch_job_filename(value_str=stdout_value, file_dir=self.logDir, batchID=batchID, guess=guess)
                stderr_path_filename = parse_batch_job_filename(value_str=stderr_value, file_dir=self.logDir, batchID=batchID, guess=guess)
                batch_log = '{0}/{1}'.format(self.logBaseURL, batch_log_filename)
                batch_stdout = '{0}/{1}'.format(self.logBaseURL, stdout_path_file_name)
                batch_stderr = '{0}/{1}'.format(self.logBaseURL, stderr_path_filename)
                workspec.set_log_file('batch_log', batch_log)
                workspec.set_log_file('stdout', batch_stdout)
                workspec.set_log_file('stderr', batch_stderr)
                batch_log_dict['batch_log'] = batch_log
                batch_log_dict['batch_stdout'] = batch_stdout
                batch_log_dict['batch_stderr'] = batch_stderr
                batch_log_dict['gtag'] = workspec.workAttributes['stdOut']
                tmpLog.debug('Done set_log_file')
                if not workspec.get_jobspec_list():
                    tmpLog.debug('No jobspec associated in the worker of workerID={0}'.format(workspec.workerID))
                else:
                    for jobSpec in workspec.get_jobspec_list():
                        # using batchLog and stdOut URL as pilotID and pilotLog
                        jobSpec.set_one_attribute('pilotID', workspec.workAttributes['stdOut'])
                        jobSpec.set_one_attribute('pilotLog', workspec.workAttributes['batchLog'])
            tmpLog.debug('Done jobspec attribute setting')

            # set data dict
            data = {'workspec': workspec,
                    'template': sdf_template,
                    'log_dir': self.logDir,
                    'n_core_per_node': n_core_per_node,
                    'panda_queue_name': panda_queue_name,
                    'x509_user_proxy': self.x509UserProxy,
                    'ce_info_dict': ce_info_dict,
                    'batch_log_dict': batch_log_dict,
                    'special_par': special_par,
                    }

            return data

        tmpLog.debug('finished preparing worker attributes')

        # map(_handle_one_worker, workspec_list)
        with ThreadPoolExecutor(self.nProcesses * 4) as thread_pool:
            dataIterator = thread_pool.map(_handle_one_worker, workspec_list)
        tmpLog.debug('{0} workers handled'.format(nWorkers))

        # exec with mcore
        with ThreadPoolExecutor(self.nProcesses) as thread_pool:
            retValList = thread_pool.map(submit_a_worker, dataIterator)
        tmpLog.debug('{0} workers submitted'.format(nWorkers))

        # propagate changed attributes
        retList = []
        for workspec, tmpVal in zip(workspec_list, retValList):
            retVal, tmpDict = tmpVal
            workspec.set_attributes_with_dict(tmpDict)
            retList.append(retVal)

        tmpLog.debug('done')

        return retList
