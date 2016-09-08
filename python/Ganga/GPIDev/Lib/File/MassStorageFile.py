from __future__ import absolute_import
##########################################################################
# Ganga Project. http://cern.ch/ganga
#
# $Id: MassStorageFile.py,v 0.1 2011-11-09 15:40:00 idzhunov Exp $
##########################################################################
import errno
import re
import os
import os.path
import copy
import glob
import inspect
import time
from fnmatch import fnmatch
from pipes import quote

from Ganga.GPIDev.Schema import Schema, Version, SimpleItem, ComponentItem

from Ganga.Utility.Config import getConfig
from Ganga.GPIDev.Base.Proxy import GPIProxyObjectFactory
from Ganga.GPIDev.Base.Proxy import stripProxy
from Ganga.Utility import Shell
from Ganga.Utility.logging import getLogger
from Ganga.GPIDev.Adapters.IGangaFile import IGangaFile
from Ganga.GPIDev.Base.Proxy import getName
from Ganga.Utility.files import expandfilename

import Ganga.Utility.Config

regex = re.compile('[*?\[\]]')
logger = getLogger()

class MassStorageFile(IGangaFile):
    """MassStorageFile represents a class marking a file to be written into mass storage (like Castor at CERN)
    """
    _schema = Schema(Version(1, 1), {'namePattern': SimpleItem(defvalue="", doc='pattern of the file name'),
                                     'localDir': SimpleItem(defvalue="", copyable=1, doc='local dir where the file is stored, used from get and put methods'),
                                     'joboutputdir': SimpleItem(defvalue="", doc='outputdir of the job with which the outputsandbox file object is associated'),
                                     'locations': SimpleItem(defvalue=[], copyable=1, typelist=[str], sequence=1, doc="list of locations where the outputfiles are uploaded"),
                                     'outputfilenameformat': SimpleItem(defvalue=None, typelist=[str, None], protected=0,\
                                                    doc="keyword path to where the output should be uploaded, i.e. /some/path/here/{jid}/{sjid}/{fname},\
                                                        if this field is not set, the output will go in {jid}/{sjid}/{fname} or in {jid}/{fname}\
                                                        depending on whether the job is split or not"),
                                     'inputremotedirectory': SimpleItem(defvalue=None, typelist=[str, None], protected=0, doc="Directory on mass storage where the file is stored"),
                                     'subfiles': ComponentItem(category='gangafiles', defvalue=[], hidden=1, sequence=1, copyable=0,\
                                                    doc="collected files from the wildcard namePattern"),
                                     'failureReason': SimpleItem(defvalue="", protected=1, copyable=0, doc='reason for the upload failure'),
                                     'compressed': SimpleItem(defvalue=False, typelist=[bool], protected=0, doc='wheather the output file should be compressed before sending somewhere')
                                     })

    _category = 'gangafiles'
    _name = "MassStorageFile"
    _exportmethods = ["location", "get", "put", "setLocation", "remove", "accessURL", "copyTo"]

    def __init__(self, namePattern='', localDir='', **kwds):
        """ namePattern is the pattern of the output file that has to be written into mass storage
        """
        super(MassStorageFile, self).__init__()
        self._setNamePath(_namePattern=namePattern, _localDir=localDir)
        self.locations = []

        self.shell = Shell.Shell()

    def __setattr__(self, attr, value):
        """
        This is an overloaded setter method to make sure that we're auto-expanding the filenames of files which exist.
        In the case we're assigning any other attributes the value is simply passed through
        Args:
            attr (str): This is the name of the attribute which we're assigning
            value (unknown): This is the value being assigned.
        """
        actual_value = value
        if attr == "namePattern":
            actual_value = os.path.basename(value)
            this_localDir = os.path.dirname(value)
            super(MassStorageFile, self).__setattr__('localDir', this_localDir)
        if attr == "localDir":
            actual_value = os.path.abspath(expandfilename(value))

        super(MassStorageFile, self).__setattr__(attr, actual_value)

    def _setNamePath(self, _namePattern='', _localDir=''):
        if _namePattern != '' and _localDir == '':
            self.namePattern = os.path.basename(_namePattern)
            self.localDir = os.path.dirname(_namePattern)
        elif _namePattern != '' and _localDir != '':
            self.namePattern = _namePattern
            self.localDir = _localDir


    def _on_attribute__set__(self, obj_type, attrib_name):
        # This is defining the object as uncopyable from outputfiles... do we want this mechanism still?
        r = copy.deepcopy(self)
        if getName(obj_type) == 'Job' and attrib_name == 'outputfiles':
            r.locations = []
            r.localDir = ''
            r.failureReason = ''
        return r

    def __repr__(self):
        """Get the representation of the file."""

        return "MassStorageFile(namePattern='%s')" % self.namePattern

    def setLocation(self):
        """
        Sets the location of output files that were uploaded to mass storage from the WN
        """
        job = self.getJobObject()

        postprocessLocationsPath = os.path.join(job.outputdir, getConfig('Output')['PostProcessLocationsFileName'])
        if not os.path.exists(postprocessLocationsPath):
            return

        def mass_line_processor(line, mass_file):
            lineParts = line.split(' ')
            pattern = lineParts[1]
            outputPath = lineParts[2]
            name = os.path.basename(outputPath).strip('.gz')
            if regex.search(mass_file.namePattern) is not None:
                if outputPath == 'ERROR':
                    logger.error("Failed to upload file to mass storage")
                    logger.error(line[line.find('ERROR') + 5:])
                    d = MassStorageFile(namePattern=pattern)
                    d.compressed = mass_file.compressed
                    d.failureReason = line[line.find('ERROR') + 5:]
                    mass_file.subfiles.append(d)
                else:
                    if pattern == self.namePattern:
                        d = MassStorageFile(namePattern=name)
                        d.compressed = mass_file.compressed
                        d.outputfilenameformat = mass_file.outputfilenameformat
                        mass_file.subfiles.append(d)
                        mass_line_processor(line, d)
            elif name == mass_file.namePattern:
                if outputPath == 'ERROR':
                    logger.error("Failed to upload file to mass storage")
                    logger.error(line[line.find('ERROR') + 5:])
                    mass_file.failureReason = line[line.find('ERROR') + 5:]
                    return
                mass_file.locations = [outputPath.strip('\n')]

        for line in open(postprocessLocationsPath, 'r'):

            if line.strip() == '':
                continue

            if line.startswith('massstorage'):
                mass_line_processor(line.strip(), self)

    def location(self):
        """
        Return list with the locations of the post processed files (if they were configured to upload the output somewhere)
        """
        tmpLocations = []
        if self.locations == []:
            if self.subfiles != []:
                for i in self.subfiles:
                    for j in i:
                        tmpLocations.append(j)
        else:
            tmpLocations = self.locations
        return tmpLocations

    def get(self, localPath=''):
        """
        Retrieves locally all files matching this MassStorageFile object pattern
        """
        self._actual_copy()

    def _actual_copy(self, localPath=''):
        """
        Copy a the file to the local storage using the get mechanism
        Args:
            localPath (str): Target path where the file is to copied to
        """
        if not localPath:
            to_location = self.localDir
            # FIXME? THIS IS POTENTIALLY VERY, VERY UNEXPECTED/UNDOCUMENTED BEHAVIOUR
            if not os.path.isdir(to_location):
                if self._getParent() is not None:
                    try:
                        new_dir = self.getJobObject().outputdir
                        if to_location:
                            logger.warning("Setting get path of file to be: '%s' from: '%s'" % (new_dir, to_location))
                        to_location = new_dir
                    except AssertionError:
                        ## CANNOT auto-make a path so lets not try to, it causes problems
                        logger.error("%s is not a valid directory.... Please set the localDir attribute" % self.localDir)
                        pass
                else:
                    logger.error("%s is not a valid directory.... Please set the localDir attribute" % self.localDir)
                    return
        else:
            to_location = localPath

        cp_cmd = getConfig('Output')['MassStorageFile']['uploadOptions']['cp_cmd']

        for location in self.locations:
            targetLocation = os.path.join(to_location, os.path.basename(location))
            os.system('%s %s %s' % (cp_cmd, location, targetLocation))

    def copyTo(self, targetPath):
        """
        Copy a the file to the local storage using the get mechanism
        Args:
            targetPath (str): Target path where the file is to copied to
        """
        self._actual_copy(targetPath)


    def getWNScriptDownloadCommand(self, indent):
        ## FIXME fix me for the situation of multiple files?

        script = """\n

###INDENT###os.system(\'###CP_COMMAND###\')

"""
        cp_cmd = '%s %s .' % (getConfig('Output')['MassStorageFile']['uploadOptions']['cp_cmd'], quote(self.locations[0]))

        replace_dict = { '###INDENT###' : indent, '###CP_COMMAND###' : cp_cmd }

        for k, v in replace_dict.iteritems():
            script = script.replace(str(k), str(v))

        return script

    def _mkdir(self, massStoragePath, exitIfNotExist=False):
        """
        Creates a folder on the mass Storage corresponding to the given path
        Args:
            massStoragePath (str): This is the path we want to make if it doesn't exist.
        """

        massStorageConfig = getConfig('Output')['MassStorageFile']['uploadOptions']
        mkdir_cmd = massStorageConfig['mkdir_cmd']
        ls_cmd = massStorageConfig['ls_cmd']

        # create the last directory (if not exist) from the config path
        pathToDirName = os.path.dirname(massStoragePath)
        dirName = os.path.basename(massStoragePath)

        directoryExists = False

        (exitcode, mystdout, mystderr) = self.execSyscmdSubprocess('%s %s' % (ls_cmd, quote(pathToDirName)))
        if exitcode != 0 and exitIfNotExist:
            self.handleUploadFailure(mystderr, '1) %s %s' % (ls_cmd, pathToDirName))
            raise GangaException(mystderr)

        for directory in mystdout.split('\n'):
            if directory.strip() == dirName:
                directoryExists = True
                break

        if not directoryExists:
            (exitcode, mystdout, mystderr) = self.execSyscmdSubprocess('%s -p %s' % (mkdir_cmd, quote(massStoragePath)))
            if exitcode != 0:
                self.handleUploadFailure(mystderr, '2) %s %s' % (mkdir_cmd, massStoragePath))
                raise GangaException(mystderr)

    def put(self):
        """
        Creates and executes commands for file upload to mass storage (Castor), this method will
        be called on the client
        """

        sourceDir = ''

        # if used as a stand alone object
        if self._getParent() is None:
            if self.localDir == '':
                _CWD = os.getcwd()
                if os.path.isfile(os.path.join(_CWD, self.namePattern)):
                    sourceDir = _CWD
                else:
                    logger.warning('localDir attribute is empty, don\'t know from which dir to take the file')
                    return
            else:
                sourceDir = self.localDir

                (result, message) = self.validate()

                if result == False:
                    logger.warning(message)
                    return

        else:
            job = self.getJobObject()
            sourceDir = job.outputdir

            # if there are subjobs, the put method will be called on every subjob
            # and will upload the resulted output file
            if len(job.subjobs) > 0:
                return

        massStorageConfig = getConfig('Output')['MassStorageFile']['uploadOptions']

        cp_cmd = massStorageConfig['cp_cmd']
        ls_cmd = massStorageConfig['ls_cmd']
        massStoragePath = massStorageConfig['path']

        try:
            self._mkdir(massStoragePath, exitIfNotExist=True)
        except GangaException:
            return

        # the folder part of self.outputfilenameformat
        folderStructure = ''
        # the file name part of self.outputfilenameformat
        filenameStructure = ''

        if self._getParent() is not None:
            jobfqid = self.getJobObject().fqid

            jobid = jobfqid
            subjobid = ''

            if (jobfqid.find('.') > -1):
                jobid = jobfqid.split('.')[0]
                subjobid = jobfqid.split('.')[1]

            if self.outputfilenameformat is None:
                filenameStructure = '{fname}'
                # create jid/sjid directories
                folderStructure = jobid
                if subjobid != '':
                    folderStructure = os.path.join(jobid, subjobid)

            else:
                filenameStructure = os.path.basename(self.outputfilenameformat)
                filenameStructure = filenameStructure.replace('{jid}', jobid)

                folderStructure = os.path.dirname(self.outputfilenameformat)
                folderStructure = folderStructure.replace('{jid}', jobid)

                if subjobid != '':
                    filenameStructure = filenameStructure.replace('{sjid}', subjobid)
                    folderStructure = folderStructure.replace('{sjid}', subjobid)
        else:
            if self.outputfilenameformat != None:
                folderStructure = os.path.dirname(self.outputfilenameformat)
                filenameStructure = os.path.basename(self.outputfilenameformat)
            else:
                filenameStructure = '{fname}'

        # create the folder structure
        if folderStructure:
            massStoragePath = os.path.join(massStoragePath, folderStructure)
            try:
                self._mkdir(massStoragePath)
            except GangaException:
                return

        # here filenameStructure has replaced jid and sjid if any, and only not
        # replaced keyword is fname
        fileName = self.namePattern
        if self.compressed:
            fileName = '%s.gz' % self.namePattern

        if regex.search(fileName) is not None:
            for currentFile in glob.glob(os.path.join(sourceDir, fileName)):
                finalFilename = filenameStructure.replace('{fname}', os.path.basename(currentFile))
                (exitcode, mystdout, mystderr) = self.execSyscmdSubprocess('%s %s %s' %\
                                                (cp_cmd, quote(currentFile), quote(os.path.join(massStoragePath, finalFilename))))

                d = MassStorageFile(namePattern=os.path.basename(currentFile))
                d.compressed = self.compressed

                if exitcode != 0:
                    self.handleUploadFailure(mystderr, '4) %s %s %s' % (cp_cmd, currentFile, os.path.join(massStoragePath, finalFilename)))
                else:
                    logger.info('%s successfully uploaded to mass storage as %s' % (currentFile, os.path.join(massStoragePath, finalFilename)))
                    d.locations = os.path.join(massStoragePath, os.path.basename(finalFilename))

                self.subfiles.append(d)
        else:
            currentFile = os.path.join(sourceDir, fileName)
            finalFilename = filenameStructure.replace('{fname}', os.path.basename(currentFile))
            (exitcode, mystdout, mystderr) = self.execSyscmdSubprocess('%s %s %s' %\
                                                        (cp_cmd, quote(currentFile), quote(os.path.join(massStoragePath, finalFilename))))
            if exitcode != 0:
                self.handleUploadFailure(mystderr, '5) %s %s %s' % (cp_cmd, currentFile, os.path.join(massStoragePath, finalFilename)))
            else:
                logger.info('%s successfully uploaded to mass storage as %s' % (currentFile, os.path.join(massStoragePath, finalFilename)))
                location = os.path.join(massStoragePath, os.path.basename(finalFilename))
                if location not in self.locations:
                    self.locations.append(location)


    def validate(self):

        # if the user has set outputfilenameformat, validate for presence of
        # jid, sjid and fname keywords depending on job type - split or
        # non-split
        if self.outputfilenameformat != None:

            searchFor = ['{fname}']
            isJob = False
            isSplitJob = False

            if self._getParent() != None:

                isJob = True

                if stripProxy(self.getJobObject()).master is not None:

                    isSplitJob = True
                    searchFor.append('{sjid}')

            missingKeywords = []

            for item in searchFor:
                if self.outputfilenameformat.find(item) == -1:
                    missingKeywords.append(item)

            if len(missingKeywords):
                return (False, 'Error in MassStorageFile.outputfilenameformat field : missing keywords %s ' % ','.join(missingKeywords))

            if isSplitJob == False and self.outputfilenameformat.find('{sjid}') > -1:
                return (False, 'Error in MassStorageFile.outputfilenameformat field :  job is non-split, but {\'sjid\'} keyword found')

            if isJob == False and self.outputfilenameformat.find('{sjid}') > -1:
                return (False, 'Error in MassStorageFile.outputfilenameformat field :  no parent job, but {\'sjid\'} keyword found')

            if isJob == False and self.outputfilenameformat.find('{jid}') > -1:
                return (False, 'Error in MassStorageFile.outputfilenameformat field :  no parent job, but {\'jid\'} keyword found')

            invalidUnixChars = ['"', ' ']
            test = self.outputfilenameformat.replace('{jid}', 'a').replace('{sjid}', 'b').replace('{fname}', 'c')

            for invalidUnixChar in invalidUnixChars:
                if test.find(invalidUnixChar) > -1:
                    return (False, 'Error in MassStorageFile.outputfilenameformat field :  invalid char %s found' % invalidUnixChar)

        return (True, '')

    def handleUploadFailure(self, error, cmd_run_str=''):
        """
        Function to display what went wrong with an associated Job id if there is one and to assign failureReason for future.
        Args:
            error (str): This is the error which was given from the shell command
            cmd_run_str (str): This is a string related to but not always exactly the command run.
        """

        self.failureReason = error
        if self._getParent() != None:
            logger.error("Job %s failed. One of the job.outputfiles couldn't be uploaded because of %s" % (str(self._getParent().fqid), self.failureReason))
        else:
            logger.error("The file can't be uploaded because of %s" % (self.failureReason))
        if cmd_run_str:
            logger.error("Attempted to run: '%s'" % (cmd_run_str))

    def getWNInjectedScript(self, outputFiles, indent, patternsToZip, postProcessLocationsFP):
        """
        Returns script that have to be injected in the jobscript for postprocessing on the WN
        """
        massStorageCommands = []

        massStorageConfig = getConfig('Output')['MassStorageFile']['uploadOptions']

        for outputFile in outputFiles:

            outputfilenameformat = 'None'
            if outputFile.outputfilenameformat != None and outputFile.outputfilenameformat != '':
                outputfilenameformat = outputFile.outputfilenameformat

            massStorageCommands.append(['massstorage', outputFile.namePattern, outputfilenameformat,
                                        massStorageConfig['mkdir_cmd'],  massStorageConfig['cp_cmd'],
                                        massStorageConfig['ls_cmd'], massStorageConfig['path']])

        script_location = os.path.join(os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe()))),
                                        'scripts/MassStorageFileWNScript.py')

        from Ganga.GPIDev.Lib.File import FileUtils
        script = FileUtils.loadScript(script_location, '###INDENT###')

        jobfqid = self.getJobObject().fqid

        jobid = jobfqid
        subjobid = ''

        if (jobfqid.find('.') > -1):
            jobid = jobfqid.split('.')[0]
            subjobid = jobfqid.split('.')[1]

        replace_dict = {'###MASSSTORAGECOMMANDS###' : repr(massStorageCommands),
                        '###PATTERNSTOZIP###' : str(patternsToZip),
                        '###INDENT###' : indent,
                        '###POSTPROCESSLOCATIONSFP###' : postProcessLocationsFP,
                        '###FULLJOBDIR###' : str(jobfqid.replace('.', os.path.sep)),
                        '###JOBDIR###' : str(jobid),
                        '###SUBJOBDIR###' : str(subjobid)}

        for k, v in replace_dict.iteritems():
            script = script.replace(str(k), str(v))

        return script

    def processWildcardMatches(self):
        if self.subfiles:
            return self.subfiles

        if regex.search(self.namePattern):
            ls_cmd = getConfig('Output')['MassStorageFile']['uploadOptions']['ls_cmd']
            exitcode, output, m = self.shell.cmd1(ls_cmd + ' ' + self.inputremotedirectory, capture_stderr=True)

            for filename in output.split('\n'):
                if fnmatch(filename, self.namePattern):
                    subfile = MassStorageFile(namePattern=filename)
                    subfile.inputremotedirectory = self.inputremotedirectory

                    self.subfiles.append(GPIProxyObjectFactory(subfile))

    def remove(self, force=False, removeLocal=False):
        """
        Removes file from remote storage ONLY by default
        """
        massStorageConfig = getConfig('Output')['MassStorageFile']['uploadOptions']
        rm_cmd = massStorageConfig['rm_cmd']

        if force == True:
            _auto_delete = True
        else:
            _auto_delete = False

        for i in self.locations:

            if not _auto_delete:

                keyin = None

                while keyin is None:
                    keyin = raw_input("Do you want to delete file %s at Location: %s ? [y/n] " % (str(self.namePattern), str(i)))
                    if keyin.lower() == 'y':
                        _delete_this = True
                    elif keyin.lower() == 'n':
                        _delete_this = False
                    else:
                        logger.warning("y/n please!")
                        keyin = None
            else:
                _delete_this = True

            if _delete_this:
                logger.info("Deleting File at Location: %s")
                self.execSyscmdSubprocess('%s %s' % (rm_cmd, quote(i)))
                self.locations.pop(i)

        if removeLocal:

            sourceDir = ''
            if self.localDir == '':
                _CWD = os.getcwd()
                if os.path.isfile(os.path.join(_CWD, self.namePattern)):
                    sourceDir = _CWD
            else:
                sourceDir = self.localDir

            _localFile = os.path.join(sourceDir, self.namePattern)

            if os.path.isfile(_localFile):

                if force:
                    _actual_delete = True
                else:

                    keyin = None
                    while keyin is None:
                        keyin = raw_input("Do you want to remove the local File: %s ? ([y]/n) " % str(_localFile))
                        if keyin.lower() in ['y', '']:
                            _actual_delete = True
                        elif keyin.lower() == 'n':
                            _actual_delete = False
                        else:
                            logger.warning("y/n please!")
                            keyin = None

                if _actual_delete:
                    remove_filename = _localFile + "_" + str(time.time()) + '__to_be_deleted_'

                    try:
                        os.rename(_localFile, remove_filename)
                    except OSError as err:
                        logger.warning("Error in first stage of removing file: %s" % remove_filename)
                        remove_filename = _localFile

                    try:
                        os.remove(remove_filename)
                    except OSError as err:
                        if err.errno != errno.ENOENT:
                            logger.error("Error in removing file: %s" % str(remove_filename))
                            raise
                        pass
        return

    def accessURL(self):

        # Need to come up with a prescription based upon the server address and
        # file on EOS or elsewhere to return a full URL which we can pass to
        # ROOT...

        protoPath = getConfig('Output')['MassStorageFile']['defaultProtocol']

        myLocations = self.location()

        accessURLs = []

        for file in myLocations:
            accessURLs.append(protoPath + os.path.join(os.sep, file))

        return accessURLs

# add MassStorageFile objects to the configuration scope (i.e. it will be
# possible to write instatiate MassStorageFile() objects via config file)
Ganga.Utility.Config.config_scope['MassStorageFile'] = MassStorageFile

