from contextlib import contextmanager
import logging

from django.core import validators
from django.core.files.base import ContentFile
from django.db import models
from django.db.models import Sum
from django.template.defaultfilters import filesizeformat
from django.urls import reverse
from django.utils.encoding import force_text
from django.utils.functional import cached_property
from django.utils.text import format_lazy
from django.utils.translation import ugettext_lazy as _

from mayan.apps.events.classes import EventManagerMethodAfter, EventManagerSave
from mayan.apps.events.decorators import method_event
from mayan.apps.lock_manager.backends.base import LockingBackend
from mayan.apps.lock_manager.decorators import locked_class_method
from mayan.apps.lock_manager.exceptions import LockError
from mayan.apps.storage.classes import DefinedStorage

from .events import (
    event_cache_created, event_cache_edited, event_cache_partition_purged,
    event_cache_purged
)
from .settings import setting_maximum_prune_attempts

logger = logging.getLogger(name=__name__)


class Cache(models.Model):
    defined_storage_name = models.CharField(
        db_index=True, help_text=_(
            'Internal name of the defined storage for this cache.'
        ), max_length=96, unique=True, verbose_name=_('Defined storage name')
    )
    maximum_size = models.BigIntegerField(
        help_text=_('Maximum size of the cache in bytes.'), validators=[
            validators.MinValueValidator(limit_value=1)
        ], verbose_name=_('Maximum size')
    )

    class Meta:
        verbose_name = _('Cache')
        verbose_name_plural = _('Caches')

    def __str__(self):
        return force_text(s=self.label)

    def get_absolute_url(self):
        return reverse(
            viewname='file_caching:cache_detail', kwargs={
                'cache_id': self.pk
            }
        )

    def get_files(self):
        return CachePartitionFile.objects.filter(partition__cache__id=self.pk)

    def get_maximum_size_display(self):
        return filesizeformat(bytes_=self.maximum_size)

    get_maximum_size_display.help_text = _(
        'Size at which the cache will start deleting old entries.'
    )
    get_maximum_size_display.short_description = _('Maximum size')

    def get_defined_storage(self):
        try:
            return DefinedStorage.get(name=self.defined_storage_name)
        except KeyError:
            return DefinedStorage(
                dotted_path='', label=_('Unknown'), name='unknown'
            )

    def get_total_size(self):
        """
        Return the actual usage of the cache.
        """
        return self.get_files().aggregate(
            file_size__sum=Sum('file_size')
        )['file_size__sum'] or 0

    def get_total_size_display(self):
        return format_lazy(
            '{} ({:0.1f}%)', filesizeformat(bytes_=self.get_total_size()),
            self.get_total_size() / self.maximum_size * 100
        )

    get_total_size_display.short_description = _('Current size')
    get_total_size_display.help_text = _('Current size of the cache.')

    @cached_property
    def label(self):
        return self.get_defined_storage().label

    def prune(self):
        """
        Deletes files until the total size of the cache is below the allowed
        maximum size of the cache.
        """
        attempts = 0
        while self.get_total_size() > self.maximum_size:
            cache_partition_file = self.get_files().earliest()
            try:
                cache_partition_file.delete()
            except LockError:
                logger.debug(
                    'Lock error trying to delete file "%s" for prune. '
                    'Skipping and attempting next file.',
                    cache_partition_file
                )
                attempts += 1

                if attempts > setting_maximum_prune_attempts.value:
                    raise RuntimeError(
                        'Too many cache prune attempts failed.'
                    )

    @method_event(
        event=event_cache_purged,
        event_manager_class=EventManagerMethodAfter,
        target='self'
    )
    def purge(self):
        """
        Deletes the entire cache.
        """
        try:
            DefinedStorage.get(name=self.defined_storage_name)
        except KeyError:
            """
            Unknown or deleted storage. Must not be purged otherwise only
            the database data will be erased but the actual storage files
            will remain.
            """
        else:
            for partition in self.partitions.all():
                partition._event_actor = getattr(self, '_event_actor', None)
                partition.purge()

    @method_event(
        event_manager_class=EventManagerSave,
        created={
            'event': event_cache_created,
            'target': 'self',
        },
        edited={
            'event': event_cache_edited,
            'target': 'self',
        }
    )
    def save(self, *args, **kwargs):
        result = super().save(*args, **kwargs)
        self.prune()
        return result

    @cached_property
    def storage(self):
        return self.get_defined_storage().get_storage_instance()


class CachePartition(models.Model):
    cache = models.ForeignKey(
        on_delete=models.CASCADE, related_name='partitions',
        to=Cache, verbose_name=_('Cache')
    )
    name = models.CharField(
        max_length=128, verbose_name=_('Name')
    )

    class Meta:
        unique_together = ('cache', 'name')
        verbose_name = _('Cache partition')
        verbose_name_plural = _('Cache partitions')

    @staticmethod
    def get_combined_filename(parent, filename):
        return '{}-{}'.format(parent, filename)

    def _lock_manager_get_lock_name(self, filename):
        return self.get_file_lock_name(filename=filename)

    @contextmanager
    def create_file(self, filename):
        lock_name = self.get_file_lock_name(filename=filename)
        try:
            logger.debug('trying to acquire lock: %s', lock_name)
            lock = LockingBackend.get_instance().acquire_lock(name=lock_name)
            logger.debug('acquired lock: %s', lock_name)
            try:
                self.cache.prune()

                # Since open "wb+" doesn't create files, force the creation
                # of an empty file.
                self.cache.storage.delete(
                    name=self.get_full_filename(filename=filename)
                )
                self.cache.storage.save(
                    name=self.get_full_filename(filename=filename),
                    content=ContentFile(content='')
                )

                partition_file = None

                try:
                    partition_file = self.files.create(filename=filename)
                    yield partition_file.open(mode='wb', _acquire_lock=False)
                except Exception as exception:
                    logger.error(
                        'Unexpected exception while trying to save new '
                        'cache file; %s', exception, exc_info=True
                    )
                    if partition_file:
                        partition_file.delete(_acquire_lock=False)
                    else:
                        # If the CachePartitionFile entry was not created
                        # do manual clean up of the empty storage file
                        # created with the previous`self.cache.storage.save`.
                        self.cache.storage.delete(
                            name=self.get_full_filename(filename=filename)
                        )
                    raise
                else:
                    partition_file.close(_acquire_lock=False)
                    partition_file.update_size(_acquire_lock=False)
            finally:
                lock.release()
        except LockError:
            logger.debug('unable to obtain lock: %s' % lock_name)
            raise

    def delete(self, *args, **kwargs):
        self.purge()
        return super().delete(*args, **kwargs)

    def get_file(self, filename):
        return self.files.get(filename=filename)

    def get_file_lock_name(self, filename):
        return 'cache_partition-file-{}-{}-{}'.format(
            self.cache.pk, self.pk, filename
        )

    def get_full_filename(self, filename):
        return CachePartition.get_combined_filename(
            parent=self.name, filename=filename
        )

    @method_event(
        event=event_cache_partition_purged,
        event_manager_class=EventManagerMethodAfter,
        target='self'
    )
    def purge(self):
        for parition_file in self.files.all():
            parition_file.delete()


class CachePartitionFile(models.Model):
    _storage_object = None

    partition = models.ForeignKey(
        on_delete=models.CASCADE, related_name='files',
        to=CachePartition, verbose_name=_('Cache partition')
    )
    datetime = models.DateTimeField(
        auto_now_add=True, db_index=True, verbose_name=_('Date time')
    )
    filename = models.CharField(max_length=255, verbose_name=_('Filename'))
    file_size = models.PositiveIntegerField(
        default=0, verbose_name=_('File size')
    )

    class Meta:
        get_latest_by = 'datetime'
        unique_together = ('partition', 'filename')
        verbose_name = _('Cache partition file')
        verbose_name_plural = _('Cache partition files')

    def _lock_manager_get_lock_name(self, *args, **kwargs):
        return self.partition.get_file_lock_name(filename=self.filename)

    @locked_class_method
    def close(self):
        if self._storage_object is not None:
            self._storage_object.close()
        self._storage_object = None

    @locked_class_method
    def delete(self, *args, **kwargs):
        self.partition.cache.storage.delete(name=self.full_filename)
        return super().delete(*args, **kwargs)

    @locked_class_method
    def exists(self):
        return self.partition.cache.storage.exists(name=self.full_filename)

    @cached_property
    def full_filename(self):
        return CachePartition.get_combined_filename(
            parent=self.partition.name, filename=self.filename
        )

    @locked_class_method
    def open(self, mode='rb'):
        # Open the file for reading. If the file is written to, the
        # .update_size() must be called.
        try:
            self._storage_object = self.partition.cache.storage.open(
                name=self.full_filename, mode=mode
            )
            return self._storage_object
        except Exception as exception:
            logger.error(
                'Unexpected exception opening the cache file; %s', exception,
                exc_info=True
            )
            raise

    @locked_class_method
    def update_size(self):
        self.file_size = self.partition.cache.storage.size(
            name=self.full_filename
        )
        self.save()
