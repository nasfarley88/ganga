import os
import re
import fnmatch

from Ganga.GPIDev.Lib.Dataset import Dataset
from Ganga.GPIDev.Schema import *
from Ganga.Utility.files import expandfilename
from Ganga.Utility.logging import getLogger
from Ganga.Utility.Config import getConfig

from dq2.info.TiersOfATLAS import _refreshToACache, ToACache, getSites
from dq2.common.DQException import *
from dq2.repository.DQRepositoryException import DQUnknownDatasetException
from dq2.location.DQLocationException import DQLocationExistsException
from dq2.common.DQException import DQInvalidRequestException
from dq2.content.DQContentException import DQInvalidFileMetadataException
from dq2.common.client.DQClientException import DQInternalServerException
from dq2.common.dao.DQDaoException import DQDaoException
from dq2.repository.DQRepositoryException import DQFrozenDatasetException

from GangaAtlas.Lib.Credentials.ProxyHelper import getNickname 
from Ganga.Core.exceptions import ApplicationConfigurationError
from Ganga.Core.GangaThread.MTRunner import MTRunner, Data, Algorithm

from GangaAtlas.Lib.Rucio import list_datasets, is_rucio_se, resolve_containers, generate_output_datasetname, \
    dataset_exists
from GangaPanda.Lib.PandaTools import get_ce_from_locations


class DQ2Dataset(Dataset):
    """ATLAS DDM Dataset

    Class that covers accessing datasets through ATLAS DDM
    """

    _schema = Schema(Version(1, 0), {
        'dataset': SimpleItem(defvalue=[], typelist=['str'], sequence=1, strict_sequence=0, doc="Dataset Name(s)"),
        'tag_info': SimpleItem(defvalue={}, doc='TAG information used to split the job'),
        'tag_files': SimpleItem(
            defvalue=[], doc='Input TAG/ELSSI files to run over. tag_info structure will get filled on submission'),
        'tag_coll_ref': SimpleItem(
            defvalue='', doc='Provide the collection ref if not in primary JOs (useful for TRF usage): AOD, ESD, RAW'),
        'tagdataset': SimpleItem(defvalue=[], typelist=['str'], sequence=1, strict_sequence=0, doc='Tag Dataset Name'),
        'use_cvmfs_tag': SimpleItem(defvalue=False, doc='Use CVMFS to access TAG files'),
        'use_aodesd_backnav': SimpleItem(defvalue=False, doc='Use AOD to ESD Backnavigation'),
        'names': SimpleItem(defvalue=[], typelist=['str'], sequence=1, doc='Logical File Names to use for processing'),
        'names_pattern': SimpleItem(defvalue=[], typelist=['str'], sequence=1,
                                    doc='Logical file name pattern to use for processing'),
        'exclude_names': SimpleItem(defvalue=[], typelist=['str'], sequence=1,
                                    doc='Logical File Names to exclude from processing'),
        'exclude_pattern': SimpleItem(defvalue=[], typelist=['str'], sequence=1,
                                      doc='Logical file name pattern to exclude from processing'),
        'number_of_files': SimpleItem(defvalue=0, doc='Number of files. '),
        'guids': SimpleItem(defvalue=[], typelist=['str'], sequence=1, doc='GUID of Logical File Names'),
        'sizes': SimpleItem(defvalue=[], typelist=['str'], sequence=1, doc='Sizes of input files'),
        'checksums': SimpleItem(defvalue=[], typelist=['str'], sequence=1,
                                doc='md5sum or adler checksums of input files'),
        'scopes': SimpleItem(defvalue=[], typelist=['str'], sequence=1,
                             doc='scopes of the input files for RUCIO testing'),
        'type': SimpleItem(defvalue='', doc='Dataset access on worker node: DQ2_LOCAL (default), DQ2_COPY, LFC'),
        'failover': SimpleItem(defvalue=False, doc='Use DQ2_COPY automatically if DQ2_LOCAL fails'),
        'datatype': SimpleItem(defvalue='', doc='Data type: DATA, MC or MuonCalibStream'),
        'accessprotocol': SimpleItem(defvalue='', doc='Accessprotocol to use on worker node, e.g. Xrootd'),
        'match_ce_all': SimpleItem(
            defvalue=False, doc='Match complete and incomplete sources of dataset to CE during job submission'),
        'min_num_files': SimpleItem(defvalue=0, doc='Number of minimum files at incomplete dataset location'),
        'check_md5sum': SimpleItem(
            defvalue=False, doc='Check md5sum of input files on storage elemenet - very time consuming !')
    })

    _category = 'datasets'
    _name = 'DQ2Dataset'
    _exportmethods = ['list_datasets', 'list_contents', 'list_locations', 'list_locations_ce',
                      'list_locations_num_files', 'get_contents', 'get_locations']

    def __init__(self):
        super(DQ2Dataset, self).__init__()

    def dataset_exists(self):
        """Check if the dataset this object represents exists in DDM

        Returns:
            bool - True if dataset exists, False if not
        """

        # do we have a dataset to start with?
        if not self.dataset:
            return False

        # ask Rucio if this is a valid dataset(s)
        all_ds_exists = True
        for dataset in self.dataset:
            all_ds_exists &= dataset_exists(dataset)

        return all_ds_exists

    def get_contents(self, overlap=True, size=False):
        '''Helper function to access dataset content'''

        allcontents = []
        diffcontents = {}
        contents_size = {}
        contents_checksum = {}
        contents_scope = {}

        datasets = resolve_containers(self.dataset)

        for dataset in datasets:
            try:
                #dq2_lock.acquire()
                try:
                    contents = dq2.listFilesInDataset(dataset, long=False)
                except:
        
                    contents = []
                    raise ApplicationConfigurationError(None,'DQ2Dataset.get_contents(): problem in call dq2.listFilesInDataset(%s, long=False)' %dataset )
                    
            finally:
                #dq2_lock.release()
                pass

            if not contents:
                contents = []
                pass

            if not len(contents):
                continue

            # Convert 0.3 output to 0.2 style
            contents = contents[0]
            contents_new = []
            for guid, info in contents.iteritems():
                # Rucio patch
                contents_new.append( (str(guid), info['lfn']) )
                contents_size[guid] = info['filesize']
                contents_checksum[guid] = info['checksum']
                contents_scope[guid] = info['scope'] 
            contents = contents_new

            # Sort contents
            def cmpfun(a,b):
                """helper function for sorting tuples"""
                return cmp(a[1],b[1])

            try:
                contents.sort(cmp=cmpfun)
            except:
                pass

            # Process only certain filenames ?
            if self.names:
                #job = self.getJobObject()
                contents = [ (guid,lfn) for guid, lfn in contents if lfn in self.names ]

            # Process only certain file name pattern ?
            if self.names_pattern:
                old_contents = contents
                contents = []
                for expattern in self.names_pattern:
                    regex = fnmatch.translate(expattern)
                    pat = re.compile(regex, re.IGNORECASE)
                    contents += [ (guid,lfn) for guid, lfn in old_contents if pat.match(lfn) and not (guid,lfn) in contents ]
                    
                    
            # Exclude certain filenames ?
            if self.exclude_names:
                #job = self.getJobObject()
                contents = [ (guid,lfn) for guid, lfn in contents if not lfn in self.exclude_names ]

            # Exclude certain file pattern ?
            if self.exclude_pattern:
                for expattern in self.exclude_pattern:
                    regex = fnmatch.translate(expattern)
                    pat = re.compile(regex, re.IGNORECASE)
                    contents = [ (guid,lfn) for guid, lfn in contents if not pat.match(lfn) ]


            # Exclude log files
            contents = [ (guid,lfn) for guid, lfn in contents if not lfn.endswith('log.tgz') ]
            pat = re.compile(r'.*log.tgz.[\w+]+$')
            contents = [ (guid,lfn) for guid, lfn in contents if not pat.match(lfn) ]

            # Process only certain number of files ?
            if self.number_of_files:
                numfiles = self.number_of_files
                if numfiles.__class__.__name__ == 'str':
                     numfiles = int(numfiles)

                if numfiles>0 and numfiles<len(contents):
                    contents_new = []
                    for i in xrange(0,numfiles):
                        contents_new.append(contents[i])

                    contents = contents_new

            allcontents = allcontents + contents
            diffcontents[dataset] = contents
            self.number_of_files = len(allcontents)

        diffcontentsNew = {}
        allcontentsSize = []
        diffcontentsSize = {}
        if size:
            # Sum up all dataset filesizes:
            sumfilesize = 0 
            for guid, lfn in allcontents:
                if guid in contents_size:
                    try:
                        sumfilesize += contents_size[guid]
                        allcontentsSize.append((guid, (lfn, contents_size[guid],contents_checksum[guid],contents_scope[guid])))
                    except:
                        pass
            # Sum up dataset filesize per dataset:
            sumfilesizeDatasets = {}
            for dataset, contents in diffcontents.iteritems():
                contentsSize = []
                tmpInfo = []
                sumfilesizeDataset = 0
                for guid, lfn in contents:
                    if guid in contents_size:
                        try:
                            sumfilesizeDataset += contents_size[guid]
                            contentsSize.append((guid, (lfn, contents_size[guid], contents_checksum[guid],contents_scope[guid])))
                        except:
                            pass
                diffcontentsNew[dataset] = (contents, sumfilesizeDataset)
                diffcontentsSize[dataset] = contentsSize
        
        if overlap:
            if size:
                return allcontentsSize
            else:
                return allcontents
        else:
            if size:
                return diffcontentsSize
            else:
                return diffcontents

    def get_locations(self, complete=0, backnav=False, overlap=True):
        '''helper function to access the dataset location'''

        alllocations = {}
        overlaplocations = []

        datasets = resolve_containers(self.dataset)
        
        for dataset in datasets:
            if backnav:
                dataset = re.sub('AOD','ESD',dataset)

            try:
                #dq2_lock.acquire()
                try:
                    locations = dq2.listDatasetReplicas(dataset)
                except:
                    logger.error('Dataset %s not found !', dataset)
                    return []
            finally:
                #dq2_lock.release()
                pass
            try:
                #dq2_lock.acquire()
                try:
                    datasetinfo = dq2.listDatasets(dataset)
                except:
                    datasetinfo = {}
            finally:
                #dq2_lock.release()
                pass

            # Rucio patch
            #if dataset.find(":")>0:
            #    try:
            #        datasettemp = dataset.split(":",1)[1]
            #    except:
            #        pass
            #    newdatasetinfo = {}
            #    newdatasetinfo[dataset] = datasetinfo[datasettemp]
            #    datasetinfo = newdatasetinfo

            try:
                datasetvuid = datasetinfo[dataset]['vuids'][0]
            except KeyError:
                try:
                    datasetvuid = datasetinfo.values()[0]['vuids'][0]
                except:
                    try:
                        datasetvuid = dq2.getMetaDataAttribute(dataset,['latestvuid'])['latestvuid']
                        import uuid
                        datasetvuid = str(uuid.UUID(datasetvuid))
                    except:
                        logger.warning('Dataset %s not found',dataset)
                        continue
                #return []

            if datasetvuid not in locations:
                logger.warning('Dataset %s not found',dataset)
                continue
                #return []
            if complete==0:
                templocations = locations[datasetvuid][0] + locations[datasetvuid][1]
            else:
                templocations = locations[datasetvuid][1]

            alllocations[dataset] = templocations

            if overlaplocations == []:
                overlaplocations = templocations

            overlaplocations_temp = []    
            for location in templocations:
                if location in overlaplocations:
                    overlaplocations_temp.append(location)
            overlaplocations = overlaplocations_temp

        if overlap:
            return overlaplocations
        else:
            return alllocations

    def list_datasets(self, name):
        '''List datasets names'''

        datasets = list_datasets(name)

        if not datasets:
            logger.error('No datasets found.')
            return

        for dsn in datasets:
            print dsn

    def list_contents(self,dataset=None):
        '''List dataset content'''

        if not dataset:
            datasets = self.dataset
        else:
            datasets = [ dataset ]

        for dataset in datasets:
            try:
                #dq2_lock.acquire()
                contents = dq2.listFilesInDataset(dataset, long=False)
            except:
                contents = {}
            finally:
                #dq2_lock.release()
                pass

            if not contents:
                print 'Dataset %s is empty.' % dataset
                return

            print 'Dataset %s' % dataset
            contents = contents[0]
            for guid, info in contents.iteritems():
                print '    %s' % info['lfn']
            print 'In total %d files' % len(contents)

    def list_locations(self,dataset=None,complete=0):
        '''List dataset locations'''

        if not dataset:
            datasets = self.dataset
        else:
            datasets = [ dataset ]

        datasets = resolve_containers(datasets)

        for dataset in datasets:
            try:
                #dq2_lock.acquire()
                try:
                    locations = dq2.listDatasetReplicas(dataset,complete)
                except DQUnknownDatasetException:
                    logger.error('Dataset %s not found !', dataset)
                    return
                except DQDaoException:
                    completestr = 'complete'
                    if not complete: completestr = 'incomplete'

                    logger.error('Dataset %s has no %s location', dataset, completestr)
                    return

            finally:
                #dq2_lock.release()
                pass

            try:
                #dq2_lock.acquire()
                datasetinfo = dq2.listDatasets(dataset)
            finally:
                #dq2_lock.release()
                pass

            # RUCIO fix
            #try:
            #    dataset = dataset.split(":",1)[1]
            #except:
            #    pass

            try:
                datasetvuid = datasetinfo[dataset]['vuids'][0]
            except:
                try:
                    datasetvuid = datasetinfo.values()[0]['vuids'][0]
                except:
                    try:
                        datasetvuid = dq2.getMetaDataAttribute(dataset,['latestvuid'])['latestvuid']
                        import uuid
                        datasetvuid = str(uuid.UUID(datasetvuid))
                    except:
                        datasetvuid = ''

            if datasetvuid not in locations:
                print 'Dataset %s not found' % dataset
                return

            locations = locations[datasetvuid]

            print 'Dataset %s' % dataset
            if len(locations[1]): print 'Complete:', ' '.join(locations[1])
            if len(locations[0]): print 'Incomplete:', ' '.join(locations[0])

    def list_locations_ce(self,dataset=None,complete=0):
        '''List the CE associated to the dataset location'''

        if not dataset:
            datasets = self.dataset
        else:
            datasets = dataset

        datasets = resolve_containers(datasets)

        for dataset in datasets:
            try:
                #dq2_lock.acquire()
                try:
                    locations = dq2.listDatasetReplicas(dataset,complete)
                except DQUnknownDatasetException:
                    logger.error('Dataset %s not found !', dataset)
                    return
            finally:
                #dq2_lock.release()
                pass

            try:
                #dq2_lock.acquire()
                datasetinfo = dq2.listDatasets(dataset)
            finally:
                #dq2_lock.release()
                pass

            try:
                datasetvuid = datasetinfo[dataset]['vuids'][0]
            except:
                try:
                    datasetvuid = datasetinfo.values()[0]['vuids'][0]
                except:
                    try:
                        datasetvuid = dq2.getMetaDataAttribute(dataset,['latestvuid'])['latestvuid']
                        import uuid
                        datasetvuid = str(uuid.UUID(datasetvuid))
                    except:
                        logger.warning('Dataset %s not found',dataset)
                        return

            if datasetvuid not in locations:
                print 'Dataset %s not found' % dataset
                return
            locations = locations[datasetvuid]

            print 'Dataset %s' % dataset
            if len(locations[1]): print 'Complete:', ' '.join(get_ce_from_locations(locations[1]))
            if len(locations[0]): print 'Incomplete:', ' '.join(get_ce_from_locations(locations[0]))

    def list_locations_num_files(self,dataset=None,complete=-1,backnav=False):
        '''List the number of files replicated to the dataset locations'''

        if not dataset:
            datasets = self.dataset
        else:
            datasets = [ dataset ]
            
        datasets = resolve_containers(datasets)

        dataset_locations_num = {}

        from Ganga.Utility.GridShell import getShell
        gridshell = getShell()
        gridshell.env['LFC_CONNTIMEOUT'] = '45'

        gridshell.env['DQ2_URL_SERVER']=config['DQ2_URL_SERVER']
        gridshell.env['DQ2_URL_SERVER_SSL']=config['DQ2_URL_SERVER_SSL']
        gridshell.env['DQ2_LOCAL_ID']=''
        import GangaAtlas.PACKAGE
        try:
            pythonpath=GangaAtlas.PACKAGE.setup.getPackagePath2('DQ2Clients','PYTHONPATH',force=False)
        except:
            pythonpath = ''
        gridshell.env['PYTHONPATH'] = gridshell.env['PYTHONPATH']+':'+pythonpath
        ## exclude the Ganga-owned external package for LFC python binding
        pythonpaths = []
        for path in gridshell.env['PYTHONPATH'].split(':'):
            if not re.match('.*\/external\/lfc\/.*', path):
                pythonpaths.append(path)
        gridshell.env['PYTHONPATH'] = ':'.join(pythonpaths)

        ## exclude any rubbish from Athena
        ld_lib_paths = []
        for path in gridshell.env['LD_LIBRARY_PATH'].split(':'):
            if not re.match('.*\/external\/lfc\/.*', path) and not re.match('.*\/sw\/lcg\/external\/.*', path):
                ld_lib_paths.append(path)
        gridshell.env['LD_LIBRARY_PATH'] = ':'.join(ld_lib_paths)

        paths = []
        for path in gridshell.env['PATH'].split(':'):
            if not re.match('.*\/external\/lfc\/.*', path) and not re.match('.*\/sw\/lcg\/external\/.*', path):
                paths.append(path)
        gridshell.env['PATH'] = ':'.join(paths)

        for dataset in datasets:
            if backnav:
                dataset = re.sub('AOD','ESD',dataset)

            locations_num = {}
            exe = os.path.join(os.path.dirname(__file__)+'/ganga-readlfc.py')        
            cmd= exe + " %s %s " % (dataset, complete) 
            rc, out, m = gridshell.cmd1(cmd,allowed_exit=[0,142])

            if rc == 0 and not out.startswith('ERROR'):
                for line in out.split():
                    if line.startswith('#'):
                        info = line[1:].split(':')
                        if len(info)==2:
                            locations_num[info[0]]=int(info[1])
            elif rc==142:
                logger.error("LFC file catalog query time out - Retrying...")
                removelfclist = ""
                while rc!=0:
                    output = out.split()
                    try:
                        removelfc = output.pop()
                        if removelfclist == "":
                            removelfclist=removelfc
                        else:
                            removelfclist= removelfclist+","+removelfc
                    except IndexError:
                        logger.error("Empty LFC string of broken catalogs")
                        return {}
                    cmd = exe + " -r " + removelfclist + " %s %s" % (dataset, complete)
                    rc, out, m = gridshell.cmd1(cmd,allowed_exit=[0,142])

                if rc == 0 and not out.startswith('ERROR'):
                    for line in out.split():
                        if line.startswith('#'):
                            info = line[1:].split(':')
                            if len(info)==2:
                                locations_num[info[0]]=int(info[1])

            dataset_locations_num[dataset] = locations_num
        return dataset_locations_num

    def get_replica_listing(self,dataset=None,SURL=True,complete=0,backnav=False):
        '''Return list of guids/surl replicated dependent on dataset locations'''
        if not dataset:
            datasets = self.dataset
        else:
            datasets = [ dataset ]

        datasets = resolve_containers(datasets)

        dataset_locations_list = {}
        for dataset in datasets:
            if backnav:
                dataset = re.sub('AOD','ESD',dataset)

            locations_list = {}
            from Ganga.Utility.GridShell import getShell
            gridshell = getShell()
            gridshell.env['LFC_CONNTIMEOUT'] = '45'
            exe = os.path.join(os.path.dirname(__file__)+'/ganga-readlfc.py')

            if SURL:
                cmd= exe + " -l %s %s " % (dataset, complete)
            else:
                cmd= exe + " -g %s %s " % (dataset, complete) 
            rc, out, m = gridshell.cmd1(cmd,allowed_exit=[0,142])

            if rc == 0 and not out.startswith('ERROR'):
                for line in out.split():
                    if line.startswith('#'):
                        info = line[1:].split(',')
                        if len(info)>1:
                            locations_list[info[0]]=info[1:]
            elif rc==142:
                logger.error("LFC file catalog query time out - Retrying...")
                removelfclist = ""
                while rc!=0:
                    output = out.split()
                    try:
                        removelfc = output.pop()
                        if removelfclist == "":
                            removelfclist=removelfc
                        else:
                            removelfclist= removelfclist+","+removelfc
                    except IndexError:
                        logger.error("Empty LFC string of broken catalogs")
                        return {}
                    cmd = exe + " -l -r " + removelfclist + " %s %s" % (dataset, complete)
                    rc, out, m = gridshell.cmd1(cmd,allowed_exit=[0,142])

                if rc == 0 and not out.startswith('ERROR'):
                    for line in out.split():
                        if line.startswith('#'):
                            info = line[1:].split(',')
                            if len(info)>1:
                                locations_list[info[0]]=info[1:]

            dataset_locations_list[dataset] = locations_list

        if dataset:
            return dataset_locations_list[dataset]
        else:
            return dataset_locations_list


class DQ2OutputDataset(Dataset):
    """DQ2 Dataset class for a dataset of output files"""
    
    _schema = Schema(Version(1,1), {
        'outputdata'     : SimpleItem(defvalue = [], typelist=['str'], sequence=1, doc='Output files to be returned via SE'), 
        'output'         : SimpleItem(defvalue = [], typelist=['str'], sequence = 1, protected=1, doc = 'Output information automatically filled by the job'),
        'isGroupDS'      : SimpleItem(defvalue = False, doc = 'Use group datasetname prefix'),
        'groupname'      : SimpleItem(defvalue='', doc='Name of the group to be used if isGroupDS=True'),
        #'datasetname'    : SimpleItem(defvalue='', copyable=0, doc='Name of the DQ2 output dataset automatically filled by the job'),
        'datasetname'    : SimpleItem(defvalue='', filter="checkNameConsistency", doc='Name of the DQ2 output dataset automatically filled by the job'),
        'datasetList'    : SimpleItem(defvalue = [], typelist=['str'],  sequence = 1,protected=1, doc='List of DQ2 output datasets automatically filled by the AthenaMC job'),
        'location'       : SimpleItem(defvalue='',doc='SE output path location'),
        'spacetoken'     : SimpleItem(defvalue='',doc='SE output spacetoken'),
        'local_location' : SimpleItem(defvalue='',doc='Local output path location'),
#        'use_datasetname' : SimpleItem(defvalue = False, doc = 'Use datasetname as it is and do not prepend users.myname.ganga'),
        'use_shortfilename' : SimpleItem(defvalue = False, doc = 'Use shorter version of filenames and do not prepend users.myname.ganga'),
        'transferredDS' : SimpleItem(defvalue='',doc='Panda only: Specify a comma-separated list of patterns so that only datasets which match the given patterns are transferred when outputdata.location is set. Either \ or "" is required when a wildcard is used. If omitted, all datasets are transferred')
        })
    
    _category = 'datasets'
    _name = 'DQ2OutputDataset'

    _exportmethods = [ 'retrieve', 'fill', 'create_dataset','create_datasets', 'dataset_exists', 'get_locations', 'create_subscription', 'clean_duplicates_in_dataset', 'clean_duplicates_in_container', 'check_content_consistency' ]

    _GUIPrefs = [ { 'attribute' : 'outputdata',     'widget' : 'String_List' },
                  { 'attribute' : 'output',         'widget' : 'String_List' },
                  { 'attribute' : 'datasetname',    'widget' : 'String' },
                  { 'attribute' : 'datasetList',    'widget' : 'String_List' },
                  { 'attribute' : 'location',       'widget' : 'String_List' },
                  { 'attribute' : 'local_location', 'widget' : 'File' },
#                  { 'attribute' : 'use_datasetname',    'widget' : 'Bool' },
                  { 'attribute' : 'isGroupDS',      'widget' : 'Bool' },
                  { 'attribute' : 'groupname',      'widget' : 'String' },
                  { 'attribute' : 'use_shortfilename',    'widget' : 'Bool' }
                  ]
    
    nameChecked = False
    dq2datasetname = ''

    def __init__(self):
        super(DQ2OutputDataset, self).__init__()

    def checkNameConsistency(self, datasetname):
        if datasetname == '':
            return ''
        elif datasetname != '':
            dq2datasetname = generate_output_datasetname(datasetname, -999 , self.isGroupDS, self.groupname)
            return dq2datasetname

    def clean_duplicates_in_dataset(self, datasetname = None, outputInfo = None):
        """Clean duplicate files from dataset if e.g. shallow retry count occured"""

        trashFiles = []
        if not datasetname:
            datasetname = self.datasetname

        logger.warning('Checking for file dulipates in %s...' %datasetname)
        
        # Get filenames from repository (actually finished jobs) 
        filenames = []
        # Use subjob outputInfo if not provided as parameter
        if not outputInfo:
            outputInfo = self.output
        for file in outputInfo:
            filenames.append(file.split(',')[1])

        # Get filenames from dataset
        contents = []
        try:
            #dq2_lock.acquire()
            try:
                contents = dq2.listFilesInDataset(datasetname, long=False)
            except:
                contents = ({},'')
                pass
        finally:
            #dq2_lock.release()
            pass

        if contents:
            contents = contents[0]

        # Convert 0.3 output to 0.2 style
        contents_new = {}
        for guid, info in contents.iteritems():
            contents_new[ info['lfn'] ] = guid 

        # Loop over all files in dataset
        for filename in contents_new.keys():
            if not filename in filenames:
                trashFiles.append(filename)

        # Determine dataset location
        try:
            #dq2_lock.acquire()
            try:
                location = dq2.listDatasetReplicas(datasetname).values()[0][1][0]
            except:
                location = datasetname.split('.')[-1]
                pass
        finally:
            #dq2_lock.release()
            pass

        if not is_rucio_se(location):
            logger.error('clean_duplicates_in_dataset failed since %s in no proper DQ2 location', location) 
            return

        # Create trash dataset
        trashFilesInfo = []
        trashFilesGuids = []
        trashDatasetname = datasetname + '.trash'
        for trashFile in trashFiles:
            guid = contents_new[trashFile]
            trashFilesGuids.append(guid)
            infoLine = trashDatasetname + ',' + trashFile + ',' + guid + ',' + '%s' %contents[guid]['filesize']  +  ',' + contents[guid]['checksum'].replace('ad:','') + ',' +  location
            trashFilesInfo.append(infoLine)
            
        if trashFiles:
            logger.warning('Removing file duplicates from %s outputdataset: %s', datasetname, trashFiles )
            try:
                self.create_dataset(trashDatasetname)
            except:
                logger.warning('Trash dataset %s already exists !', trashDatasetname )
            
            self.register_datasets_details( trashDatasetname, trashFilesInfo)
            logger.warning('Duplicate files are now in dataset: %s', trashDatasetname)
            # Delete duplicate files from original dataset
            try:
                #dq2_lock.acquire()
                try:
                    dq2.deleteFilesFromDataset(datasetname, trashFilesGuids)
                except:
                    logger.error('Failure during removal of duplicates from dataset %s', datasetname)
                    pass
            finally:
                #dq2_lock.release()
                pass

            # Delete trash dataset
            if config['DELETE_DUPLICATES_DATASET']:
                try:
                    #dq2_lock.acquire()
                    try:
                        dq2.deleteDatasetReplicas(trashDatasetname, location)                    
                    except:
                        logger.error('Failure during removal of duplicates dataset %s', trashDatasetname)
                        pass
                finally:                                                        
                    #dq2_lock.release()
                    pass

        return

    def clean_duplicates_in_container(self, containername = None):
        """Clean duplicate files from container and its datasets if e.g. shallow retry count occured"""

        if not containername:
            containername = self.datasetname

        logger.warning('Checking for file dulipates in %s...' %containername)

        if not containername.endswith('/'):
            logger.warning('%s is not a dataset container - doing nothing!' %containername)
            return
       
        # Resolved container into datasets
        datasets = resolve_containers([containername])
        # Use master job info
        outputInfo = self.output
        # Clean all dataset individually
        for dataset in datasets:
            self.clean_duplicates_in_dataset(dataset, outputInfo )

        return

    def dataset_exists(self, datasetname = None):
        """Check if dataset already exists"""
        exist = False
        if not datasetname: datasetname=self.datasetname
        try:
            #dq2_lock.acquire()
            try:
                content = dq2.listDatasets(datasetname)
            except:
                content = []
        finally:
            #dq2_lock.release()
            pass
        if len(content)>0:
            exist = True
            
        return exist

    def get_locations(self, datasetname = None, complete=0, quiet = False):
        '''helper function to access the dataset location'''

        if not datasetname: datasetname=self.datasetname

        try:
            #dq2_lock.acquire()
            try:
                locations = dq2.listDatasetReplicas(datasetname)
            except:
                logger.error('Dataset %s not found !', datasetname)
                return
        finally:
            #dq2_lock.release()
            pass
        try:
            #dq2_lock.acquire()
            datasetinfo = dq2.listDatasets(datasetname)
        finally:
            #dq2_lock.release()
            pass

        try:
            datasetvuid = datasetinfo[datasetname]['vuids'][0]
        except:
            try:
                datasetvuid = datasetinfo.values()[0]['vuids'][0]
            except:
                try:
                    datasetvuid = dq2.getMetaDataAttribute(datasetname,['latestvuid'])['latestvuid']
                    import uuid
                    datasetvuid = str(uuid.UUID(datasetvuid))
                except:
                    logger.warning('Dataset %s not found',datasetname)

        if datasetvuid not in locations:
            logger.warning('Dataset %s not found',datasetname)
            return []
        if complete==0:
            return locations[datasetvuid][0] + locations[datasetvuid][1]
        else:
            return locations[datasetvuid][1]

    def create_dataset(self, datasetname = None):
        """Create dataset in central DQ2 database"""

        if datasetname:
            try:
                #dq2_lock.acquire()
                dq2.registerNewDataset(datasetname)
            finally:
                #dq2_lock.release()
                pass

            self.datasetname = datasetname

    def create_datasets(self, datasets):
        # first, ensure uniqueness of name
        for dataset in datasets:
            if dataset not in self.datasetList:
                self.datasetList.append(dataset)
        for dataset in self.datasetList:
            try:
                #dq2_lock.acquire()
                content = dq2.listDatasets(dataset)
            finally:
                #dq2_lock.release()
                pass

            if len(content)>0:
                logger.warning("dataset %s already exists: skipping", dataset)
                continue
            logger.debug("creating dataset: %s", dataset)
            self.create_dataset(dataset)
        
        self.datasetname="" # mandatory to avoid confusing the fill method
        return

    def create_subscription(self, datasetname = None, location = None):
        """Create a subscription for a dataset"""
        if datasetname and location:
            if is_rucio_se(location) and \
                   (location.find('LOCALGROUPDISK')>0 or location.find('SCRATCHDISK')>0):
                try:
                    #dq2_lock.acquire()
                    dq2.registerDatasetSubscription(datasetname, location)
                    logger.warning('Dataset %s has been subscribed to %s.', datasetname, location)
                finally:
                    #dq2_lock.release()
                    pass
                    
        return

    def register_dataset_location(self, datasetname, siteID):
        """Register location of dataset into DQ2 database"""
        alllocations = []

        try:
            #dq2_lock.acquire()
            try:
                datasetinfo = dq2.listDatasets(datasetname)
            except:
                datasetinfo = {}
        finally:
            #dq2_lock.release()
            pass

        if datasetinfo=={}:
            logger.error('Dataset %s is not defined in DQ2 database !' , datasetname )
            return -1
        
        try:
            #dq2_lock.acquire()
            try:
                locations = dq2.listDatasetReplicas(datasetname)
            except:
                locations = {}
        finally:
            #dq2_lock.release()
            pass

        if locations != {}: 
            try:
                datasetvuid = datasetinfo[datasetname]['vuids'][0]
            except KeyError:
                logger.error('Dataset %s not found', datasetname )
                return -1
            if datasetvuid not in locations:
                logger.error( 'Dataset %s not found', datasetname )
                return -1
            alllocations = locations[datasetvuid][0] + locations[datasetvuid][1]

        try:
            #dq2_lock.acquire()
            if not siteID in alllocations:
                try:
                    dq2.registerDatasetLocation(datasetname, siteID)
                except DQInvalidRequestException as Value:
                    logger.error('Error registering location %s of dataset %s: %s', datasetname, siteID, Value) 
        finally:
            #dq2_lock.release()
            pass

        # Verify registration
        try:
            #dq2_lock.acquire()
            try:
                locations = dq2.listDatasetReplicas(datasetname)
            except:
                locations = {}
        finally:
            #dq2_lock.release()
            pass

        if locations != {}: 
            datasetvuid = datasetinfo[datasetname]['vuids'][0]
            alllocations = locations[datasetvuid][0] + locations[datasetvuid][1]
        else:
            alllocations = []

        return alllocations


    def register_file_in_dataset(self,datasetname,lfn,guid, size, checksum):
        """Add file to dataset into DQ2 database"""
        # Check if dataset really exists

        try:
            #dq2_lock.acquire()
            content = dq2.listDatasets(datasetname)
        finally:
            #dq2_lock.release()
            pass

        if content=={}:
            logger.error('Dataset %s is not defined in DQ2 database !',datasetname)
            return
        # Add file to DQ2 dataset
        ret = []
        #sizes = []
        #checksums = []
        #for i in xrange(len(lfn)):
        #    sizes.append(None)
        #    checksums.append(None)
        
        try:
            #dq2_lock.acquire()
            try:
                ret = dq2.registerFilesInDataset(datasetname, lfn, guid, size, checksum) 
            except (DQInvalidFileMetadataException, DQInvalidRequestException, DQFrozenDatasetException) as Value:
                logger.warning('Warning, some files already in dataset or dataset is frozen: %s', Value)
                pass
        finally:
            #dq2_lock.release()
            pass

        return 

    def register_datasets_details(self,datasets,outdata):

        reglines=[]
        for line in outdata:
            try:
                #[dataset,lfn,guid,siteID]=line.split(",")
                [dataset,lfn,guid,size,md5sum,siteID]=line.split(",")
            except ValueError:
                continue
            size = long(size)
            adler32='ad:'+md5sum
            if len(md5sum)==32:
                adler32='md5:'+md5sum
            
            siteID=siteID.strip() # remove \n from last component
            regline=dataset+","+siteID
            if regline in reglines:
                logger.debug("Registration of %s in %s already done, skipping" % (dataset,siteID))
                #continue
            else:
                reglines.append(regline)
                logger.info("Registering dataset %s in %s" % (dataset,siteID))
                # use another version of register_dataset_location, as the "secure" one does not allow to keep track of datafiles saved in the fall-back site (CERNCAF)
                try:
                    #dq2_lock.acquire()
                    content = dq2.listDatasets(dataset)
                finally:
                    #dq2_lock.release()
                    pass

                if content=={}:
                    logger.error('Dataset %s is not defined in DQ2 database !',dataset)
                else: 
                    try:
                        #dq2_lock.acquire()
                        try:
                            dq2.registerDatasetLocation(dataset, siteID)
                        except (DQLocationExistsException, DQInternalServerException):
                            logger.debug("Dataset %s is already registered at location %s", dataset, siteID )
                        
                    finally:
                        #dq2_lock.release()
                        pass

            self.register_file_in_dataset(dataset,[lfn],[guid],[size],[adler32])

    def check_content_consistency(self, numsubjobs, **options ):
        """Check outputdataset consistency"""
        
        # Resolve container into datasets
        datasets = resolve_containers([self.datasetname])

        for dataset in datasets:
            try:
                #dq2_lock.acquire()
                try:
                    contents = dq2.listFilesInDataset(dataset, long=False)
                except:
                    contents = []
                    pass
            finally:
                #dq2_lock.release()
                pass

            if not contents:
                contents = []
                pass

            if not len(contents):
                continue
       
            contents = contents[0]
            contents_new = []
            contents_files = []
            for guid, info in contents.iteritems():
                contents_new.append( (guid, info['lfn']) )
                contents_files.append( info['lfn'] ) 
            contents = contents_new

            numoutputfiles = len(contents)
            
            # Master job
            if numsubjobs:
                numrequestedoutputfiles = len(self.outputdata)*numsubjobs
                if not numrequestedoutputfiles == numoutputfiles:
                    logger.warning('%s output files in outputdataset %s is not consistent with %s subjobs and %s requested outputfiles - Please carefully check output !', numoutputfiles, dataset,numsubjobs, numrequestedoutputfiles)
            # Subjob
            else:
                for outputinfo in self.output:
                    filename = outputinfo.split(',')[1]
                    if not filename in contents_files:
                        logger.warning('output file %s is not in outputdataset %s - Please carefully check output !', filename, dataset)

        return

    def fill(self, type=None, name=None, **options ):
        """Determine outputdata and outputsandbox locations of finished jobs
        and fill output variable"""

        from Ganga.GPIDev.Lib.Job import Job
        from GangaAtlas.Lib.ATLASDataset.ATLASDataset import filecheck

        job = self._getParent()

        # Determine local output path to store files
        if job.outputdata.local_location:
            outputlocation = expandfilename(job.outputdata.local_location)
        elif job.outputdata.location and (job.backend._name in [ 'Local', 'LSF', 'PBS', 'SGE']):
            outputlocation = expandfilename(job.outputdata.location)
        else:
            try:
                tmpdir = os.environ['TMPDIR']
            except:
                tmpdir = '/tmp/'
            outputlocation = tmpdir

        # Output files on SE
        outputfiles = job.outputdata.outputdata
        
        # Search output_guid files from LCG jobs in outputsandbox
        jobguids = []

        if job.backend._name in [ 'LCG', 'CREAM', 'Local', 'LSF', 'PBS', 'SGE']:
            pfn = job.outputdir + "output_guids"
            fsize = filecheck(pfn)
            if (fsize>0):
                jobguids.append(pfn)
                logger.debug('jobguids: %s', jobguids)
                
            
            # Get guids from output_guid files
            for ijobguids in jobguids: 
                f = open(ijobguids)
                templines =  [ line.strip() for line in f ]
                if not self.output:
                    for templine in templines:
                        tempguid = templine.split(',')
                        #self.output = self.output + tempguid

                f.close()
                
            # Get output_location
            pfn = job.outputdir + "output_location"
            fsize = filecheck(pfn)
            if (fsize>0):
                f = open(pfn)
                line = f.readline()
                self.location = line.strip()
                f.close()
                
                #  Register DQ2 location
                # FMB: protection against empty strings
                if self.datasetname and not (job.application._name in ['Athena', 'AthenaTask'] and job.backend._name in [ 'LCG', 'CREAM', 'Local', 'LSF', 'PBS', 'SGE']):
                    self.register_dataset_location(self.datasetname, self.location)
                    
            pfn = job.outputdir + "output_data"
            fsize = filecheck(pfn)
            if fsize>0:
                f=open(pfn)
                for line in f.readlines():
                    self.output.append( line.strip() )
                f.close()

            # Extract new dataset name and fill it into repository
            for outputInfo in self.output:
                datasetnameTemp = outputInfo.split(',')[0]
                try:
                    if not datasetnameTemp.endswith('.' + self.location):
                        datasetnameComp = self.datasetname + '.' + self.location
                    else:
                        datasetnameComp = self.datasetname
                except:
                    datasetnameComp = self.datasetname + '.' + outputInfo.split(',')[5]
                match = re.search(datasetnameComp, datasetnameTemp)
                if match:
                    logger.debug('Changed outputdata.dataset from %s to %s', self.datasetname, datasetnameTemp)
                    self.datasetname = datasetnameTemp
                    # Work around for failing location registration on worker node
                    out = self.register_dataset_location(self.datasetname, self.location)
                    if not self.location in out:
                        logger.error('Error during dataset location registration of %s at %s', self.datasetname, self.location)
                
        # Local host execution
        if (job.backend._name in [ 'Local', 'LSF', 'PBS', 'SGE']): 
            for file in outputfiles:
                pfn = outputlocation+"/"+file
                fsize = filecheck(pfn)
                if (fsize>0):
                    self.output.append(pfn)

        # Output files in the sandbox 
        outputsandboxfiles = job.outputsandbox
        for file in outputsandboxfiles:
            pfn = job.outputdir+"/"+file
            fsize = filecheck(pfn)
            if (fsize>0):
                self.output.append(pfn)

        # Set Replica lifteime
        set_dataset_lifetime(self.datasetname, self.location)

        # Master job finish
        if not job.master and job.subjobs:
            self.location = []
            self.output = []
            self.allDatasets = []
            for subjob in job.subjobs:
                self.output+=subjob.outputdata.output
                self.datasetname=subjob.outputdata.datasetname
                self.location.append(subjob.outputdata.location)
                if not subjob.outputdata.datasetname in self.allDatasets:
                    for outputInfo in subjob.outputdata.output:
                        if len(outputInfo.split(','))>1:
                            datasetnameTemp = outputInfo.split(',')[0]
                            if not datasetnameTemp in self.allDatasets:
                                self.allDatasets.append(datasetnameTemp)
                                
            if (job.application._name in ['Athena','AthenaTask'] and job.backend._name in [ 'LCG', 'CREAM', 'Local', 'LSF', 'PBS', 'SGE']):
                newDatasetname = job.outputdata.datasetname
                for dataset in self.allDatasets:
                    # Clean dataset from duplicates on LCG backend
                    if config['CHECK_OUTPUT_DUPLICATES'] and job.backend._name in [ 'LCG' ]:
                        self.clean_duplicates_in_dataset(dataset)
                    # output container name
                    for location in self.location:
                        if location:
                            match = re.search('^(\S*).%s.*'%location, dataset)
                        else:
                            match = re.search('^(\S*)\..*', dataset)
                        if match:
                            newDatasetname = match.group(1)
                            break
                        
                # Create output container
                containerName = newDatasetname+'/'
                #try:
                #    dq2_lock.acquire()
                #    try:
                #        dq2.registerContainer(containerName)
                #    except:
                #        logger.warning('Problem registering container %s', containerName)
                #        pass
                #finally:
                #    dq2_lock.release()
                try:
                    #dq2_lock.acquire()
                    for dataset in self.allDatasets:
                        try:
                            dq2.freezeDataset(dataset)
                        except DQFrozenDatasetException:
                            pass
                finally:
                    #dq2_lock.release()
                    pass

                #try:
                #    dq2_lock.acquire()
                #    try:
                #        dq2.registerDatasetsInContainer(containerName, self.allDatasets)
                #    except:
                #        logger.warning('Problem registering datasets %s in container %s',  self.allDatasets, containerName)
                #        pass
                #finally:
                #    dq2_lock.release()

                self.datasetname = containerName
        else:
            # AthenaMC: register dataset location and insert file in dataset only within subjobs (so that if one subjob fails, the master job fails, but the dataset is saved...). Master job completion does not do anything...
            if not (job.application._name in ['Athena', 'AthenaTask'] and job.backend._name in [ 'LCG', 'CREAM', 'Local', 'LSF', 'PBS', 'SGE']):
                self.register_datasets_details(self.datasetname,self.output)
            elif not job.master and not job.subjobs:
                self.allDatasets = [ ]
                for outputInfo in self.output:
                    datasetnameTemp = outputInfo.split(',')[0]
                    if not datasetnameTemp in self.allDatasets:
                        self.allDatasets.append(datasetnameTemp)
                for datasetnameFreeze in self.allDatasets:
                    try:
                        #dq2_lock.acquire()
                        try:
                            dq2.freezeDataset(datasetnameFreeze)
                        except DQFrozenDatasetException:
                            pass
                    finally:
                        #dq2_lock.release()
                        pass
                

    def retrieve(self, type=None, name=None, **options ):
        """Retrieve files listed in outputdata and registered in output from
        remote SE to local filesystem in background thread"""
        from Ganga.GPIDev.Lib.Job import Job
        from GangaAtlas.Lib.ATLASDataset import Download
        import os, threading

        subjobDownload = options.get('subjobDownload')
        blocking = options.get('blocking')
        use_dsname = options.get('useDSNameForDir')
        output_names_re = options.get('outputNamesRE')
        thread_pool = options.get('threadPool')

        job = self._getParent()

        # Master job finish
        if not job.master and job.subjobs:
            masterJob = True
        else:
            masterJob = False

        # call the subjob retrieve method if available
        if len(job.subjobs) > 0 and subjobDownload:
            
            thread_pool = DQ2OutputDownloader(numThread = config['NumberOfDQ2DownloadThreads'])
            for sj in job.subjobs:
                sj.outputdata.retrieve(blocking=False, useDSNameForDir=use_dsname, outputNamesRE=output_names_re, threadPool=thread_pool)

            thread_pool.start()

            if blocking:
                thread_pool.join()
            return

        os.environ['DQ2_URL_SERVER'] = config['DQ2_URL_SERVER']
        os.environ['DQ2_URL_SERVER_SSL'] = config['DQ2_URL_SERVER_SSL']
        
        if 'DQ2_LOCAL_ID' not in os.environ:
            os.environ['DQ2_LOCAL_ID'] = "DUMMY"
        if 'DQ2_COPY_COMMAND' not in os.environ:
            os.environ['DQ2_COPY_COMMAND']="lcg-cp --vo atlas"

        if (job.outputdata.outputdata and job.backend._name in [ 'LCG', 'CREAM'] and job.outputdata.output) or (job.backend._name == 'Panda'):
            # Determine local output path to store files
            local_location = options.get('local_location')

            if job._getRoot().subjobs:
                id = "%d" % (job._getRoot().id)
            else:
                id = "%d" % job.id

            if local_location:
                outputlocation = expandfilename(local_location)
                if not use_dsname:
                    try:
                        outputlocation = os.path.join( outputlocation, id )
                        os.makedirs(outputlocation)
                    except OSError:
                        pass
            elif job.outputdata.local_location:
                outputlocation = expandfilename(job.outputdata.local_location)
                if not use_dsname:
                    try:
                        outputlocation = os.path.join( outputlocation, id )
                        os.makedirs(outputlocation)
                    except OSError:
                        pass
            else:
                # User job repository location
                outputlocation = job.outputdir

            # Use single download if called from master job
            if masterJob and not subjobDownload:
                if not use_dsname:
                    exe = 'dq2-get --client-id=ganga -L ROAMING -a -d -D '
                    temp_location = outputlocation
                else:
                    exe = 'dq2-get --client-id=ganga -L ROAMING -a -d '
                    temp_location = os.path.join(outputlocation, job.outputdata.datasetname)
                    

                if job.backend._name == 'Panda':
                    cmd = '%s -H %s %s' %(exe, temp_location, job.outputdata.datasetname)
                else:
                    cmd = '%s -H %s %s' %(exe, temp_location, job.outputdata.datasetname)
                
                logger.warning("Please be patient - background execution of dq2-get of %s to %s", job.outputdata.datasetname, temp_location )

                threads=[]
                thread = Download.download_dq2(cmd)
                thread.setDaemon(True)
                thread.start()
                threads.append(thread)
            
            else: # User download per subjob 
                # loop over all filenames
                if job.outputdata.output:
                    filenames = job.outputdata.output
                    for fileinfo in filenames:
                        filename = fileinfo.split(',')[1]

                        # re if required
                        if output_names_re and not re.search(output_names_re, filename):
                            continue
                        
                        if not use_dsname:
                            exe = 'dq2-get --client-id=ganga -L ROAMING -a -d -D '
                            temp_location = outputlocation
                        else:
                            exe = 'dq2-get --client-id=ganga -L ROAMING -a -d '
                            temp_location = os.path.join(outputlocation, job.outputdata.datasetname)

                        if job.backend._name == 'Panda':
                            cmd = '%s -H %s -f %s %s' %(exe, temp_location, filename, job.outputdata.datasetname)
                        else:
                            cmd = '%s -s %s -H %s -f %s %s' %(exe, job.outputdata.location, temp_location, filename, job.outputdata.datasetname)

                        logger.warning("Please be patient - background execution of dq2-get of %s to %s", job.outputdata.datasetname, temp_location )
                        if thread_pool:
                            thread_pool.addTask(cmd)
                        else:
                            threads=[]
                            thread = Download.download_dq2(cmd)
                            thread.setDaemon(True)
                            thread.start()
                            threads.append(thread)
                else:
                    logger.warning('job.outputdata.output emtpy - nothing to download')

            if blocking:
                for thread in threads:
                    thread.join()
                                        
            #for thread in threads:
            #    thread.join()

        else:
            logger.error("Nothing to download")


class DQ2OutputDownloadTask:
    """
    Class for defining a data object for each output downloading task.
    """

    _attributes = ('cmd')

    def __init__(self, cmd):
        self.cmd = cmd


class DQ2OutputDownloadAlgorithm(Algorithm):
    """
    Class for implementing the logic of each downloading task.
    """

    def process(self, item):
        """
        downloads output of one DQ2 job
        """
        from GangaAtlas.Lib.ATLASDataset import Download

        thread = Download.download_dq2(item.cmd)
        thread.setDaemon(True)
        thread.start()
        thread.join()

        return True

class DQ2OutputDownloader(MTRunner):

    """
    Class for managing multi-threaded downloading of DQ2 Output
    """

    def __init__(self, numThread=5):

        MTRunner.__init__(self, name='dq2_output_downloader', data=Data(collection=[]), algorithm=DQ2OutputDownloadAlgorithm())

        #self.keepAlive = True
        self.numThread = numThread

    def countAliveAgent(self):

        return self.__cnt_alive_threads__()

    def addTask(self, cmd):
        task = DQ2OutputDownloadTask(cmd)
        self.addDataItem(task)

        return True


logger = getLogger()

# New for DQ2 client 2.3.0
from Ganga.GPIDev.Credentials import GridProxy
gridProxy = GridProxy()
username_global = gridProxy.identity(safe=True)
nickname = getNickname(allowMissingNickname=False)
if nickname:
    username_global = nickname
os.environ['RUCIO_ACCOUNT'] = username_global
logger.debug("Using RUCIO_ACCOUNT = %s " %(os.environ['RUCIO_ACCOUNT'])) 

from dq2.clientapi.DQ2 import DQ2
dq2=DQ2(force_backend='rucio')

from threading import Lock
dq2_lock = Lock()

from Ganga.GPIDev.Credentials import GridProxy
gridProxy = GridProxy()

config = getConfig('DQ2')

baseURLDQ2 = config['DQ2_URL_SERVER']
baseURLDQ2SSL = config['DQ2_URL_SERVER_SSL']
   
verbose = False
