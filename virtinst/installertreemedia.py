#
# Copyright 2006-2009, 2013, 2014 Red Hat, Inc.
#
# This work is licensed under the GNU GPLv2 or later.
# See the COPYING file in the top-level directory.

import logging
import os

from . import progress
from . import urldetect
from . import urlfetcher
from .devices import DeviceDisk
from .installerinject import perform_initrd_injections
from .kernelupload import upload_kernel_initrd
from .osdict import OSDB


# Enum of the various install media types we can have
(MEDIA_DIR,
 MEDIA_ISO,
 MEDIA_URL) = range(1, 4)


def _is_url(url):
    return (url.startswith("http://") or
            url.startswith("https://") or
            url.startswith("ftp://"))


class _LocationData(object):
    def __init__(self, os_variant, kernel_pairs, os_media):
        self.os_variant = os_variant
        self.kernel_pairs = kernel_pairs
        self.os_media = os_media

        self.kernel_url_arg = None
        if self.os_variant:
            osobj = OSDB.lookup_os(self.os_variant)
            self.kernel_url_arg = osobj.get_kernel_url_arg()


class InstallerTreeMedia(object):
    """
    Class representing --location Tree media. Can be one of

      - A network URL: http://dl.fedoraproject.org/...
      - A local directory
      - A local .iso file, which will be accessed with isoinfo
    """

    @staticmethod
    def validate_path(conn, path):
        try:
            dev = DeviceDisk(conn)
            dev.device = dev.DEVICE_CDROM
            dev.path = path
            dev.validate()
            return dev.path
        except Exception as e:
            logging.debug("Error validating install location", exc_info=True)
            if path.startswith("nfs:"):
                logging.warning("NFS URL installs are no longer supported. "
                    "Access your install media over an alternate transport "
                    "like HTTP, or manually mount the NFS share and install "
                    "from the local directory mount point.")

            raise ValueError(_("Validating install media '%s' failed: %s") %
                (str(path), e))

    @staticmethod
    def get_system_scratchdir(guest):
        """
        Return the tmpdir that's accessible by VMs on system libvirt URIs
        """
        if guest.conn.is_xen():
            return "/var/lib/xen"
        return "/var/lib/libvirt/boot"

    @staticmethod
    def make_scratchdir(guest):
        """
        Determine the scratchdir for this URI, create it if necessary.
        scratchdir is the directory that's accessible by VMs
        """
        user_scratchdir = os.path.join(
                guest.conn.get_app_cache_dir(), "boot")
        system_scratchdir = InstallerTreeMedia.get_system_scratchdir(guest)

        # If we are a session URI, or we don't have access to the system
        # scratchdir, make sure the session scratchdir exists and use that.
        if (guest.conn.is_session_uri() or
            not os.path.exists(system_scratchdir) or
            not os.access(system_scratchdir, os.W_OK)):
            if not os.path.exists(user_scratchdir):
                os.makedirs(user_scratchdir, 0o751)  # pragma: no cover
            return user_scratchdir

        return system_scratchdir  # pragma: no cover

    def __init__(self, conn, location, location_kernel, location_initrd):
        self.conn = conn
        self.location = location
        self._location_kernel = location_kernel
        self._location_initrd = location_initrd
        self.initrd_injections = []

        if location_kernel or location_initrd:
            if not location:
                raise ValueError(_("location kernel/initrd may only "
                    "be specified with a location URL/path"))
            if not (location_kernel and location_initrd):
                raise ValueError(_("location kernel/initrd must be "
                    "be specified as a pair"))

        self._cached_fetcher = None
        self._cached_data = None

        self._tmpfiles = []
        self._tmpvols = []

        self._media_type = MEDIA_ISO
        if (not self.conn.is_remote() and
            os.path.exists(self.location) and
            os.path.isdir(self.location)):
            self.location = os.path.abspath(self.location)
            self._media_type = MEDIA_DIR
        elif _is_url(self.location):
            self._media_type = MEDIA_URL

        if self.conn.is_remote() and not self._media_type == MEDIA_URL:
            raise ValueError(_("Cannot access install tree on remote "
                "connection: %s") % self.location)

        if self._media_type == MEDIA_ISO:
            InstallerTreeMedia.validate_path(self.conn, self.location)


    ########################
    # Install preparations #
    ########################

    def _get_fetcher(self, guest, meter):
        meter = progress.ensure_meter(meter)

        if not self._cached_fetcher:
            scratchdir = InstallerTreeMedia.make_scratchdir(guest)

            self._cached_fetcher = urlfetcher.fetcherForURI(
                self.location, scratchdir, meter)

        self._cached_fetcher.meter = meter
        return self._cached_fetcher

    def _get_cached_data(self, guest, fetcher):
        if not self._cached_data:
            has_location_kernel = bool(
                    self._location_kernel and self._location_initrd)
            store = urldetect.getDistroStore(guest, fetcher,
                    skip_error=has_location_kernel)

            os_variant = None
            os_media = None
            kernel_paths = []
            if store:
                kernel_paths = store.get_kernel_paths()
                os_variant = store.get_osdict_info()
                os_media = store.get_os_media()
            if has_location_kernel:
                kernel_paths = [
                        (self._location_kernel, self._location_initrd)]

            self._cached_data = _LocationData(os_variant, kernel_paths,
                    os_media)
        return self._cached_data

    def _prepare_kernel_url(self, guest, cache, fetcher):
        def _check_kernel_pairs():
            for kpath, ipath in cache.kernel_pairs:
                if fetcher.hasFile(kpath) and fetcher.hasFile(ipath):
                    return kpath, ipath
            raise RuntimeError(  # pragma: no cover
                    _("Couldn't find kernel for install tree."))

        kernelpath, initrdpath = _check_kernel_pairs()
        kernel = fetcher.acquireFile(kernelpath)
        self._tmpfiles.append(kernel)
        initrd = fetcher.acquireFile(initrdpath)
        self._tmpfiles.append(initrd)

        perform_initrd_injections(initrd,
                                  self.initrd_injections,
                                  fetcher.scratchdir)

        system_scratchdir = InstallerTreeMedia.get_system_scratchdir(guest)
        kernel, initrd, tmpvols = upload_kernel_initrd(
                guest.conn, fetcher.scratchdir, system_scratchdir,
                fetcher.meter, kernel, initrd)
        self._tmpvols += tmpvols

        return kernel, initrd


    ##############
    # Public API #
    ##############

    def _prepare_unattended_data(self, guest, script):
        unattended_cmdline = script.generate_cmdline()
        logging.debug("Generated unattended cmdline: %s", unattended_cmdline)

        expected_filename = script.get_expected_filename()
        scriptpath = script.write(guest)
        self._tmpfiles.append(scriptpath)
        self.initrd_injections.append((scriptpath, expected_filename))
        return unattended_cmdline

    def prepare(self, guest, meter, unattended_script):
        fetcher = self._get_fetcher(guest, meter)
        cache = self._get_cached_data(guest, fetcher)

        kernel_args = ""
        if unattended_script:
            kernel_args = self._prepare_unattended_data(
                    guest, unattended_script)
        elif self.is_network_url() and cache.kernel_url_arg:
            kernel_args = "%s=%s" % (cache.kernel_url_arg, self.location)

        kernel, initrd = self._prepare_kernel_url(guest, cache, fetcher)
        return kernel, initrd, kernel_args or ""

    def cleanup(self, guest):
        ignore = guest
        for f in self._tmpfiles:
            logging.debug("Removing %s", str(f))
            os.unlink(f)

        for vol in self._tmpvols:
            logging.debug("Removing volume '%s'", vol.name())
            vol.delete(0)

        self._tmpvols = []
        self._tmpfiles = []

    def cdrom_path(self):
        if self._media_type in [MEDIA_ISO]:
            return self.location

    def is_network_url(self):
        if self._media_type in [MEDIA_URL]:
            return self.location

    def detect_distro(self, guest):
        fetcher = self._get_fetcher(guest, None)
        cache = self._get_cached_data(guest, fetcher)
        return cache.os_variant

    def get_os_media(self, guest, meter):
        fetcher = self._get_fetcher(guest, meter)
        cache = self._get_cached_data(guest, fetcher)
        return cache.os_media

    def requires_internet(self, guest, meter):
        if self._media_type in [MEDIA_URL, MEDIA_DIR]:
            return True

        os_media = self.get_os_media(guest, meter)
        if os_media:
            return os_media.is_netinst()
        return False
