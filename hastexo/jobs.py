import time

from django.db import transaction
from django.utils import timezone
from heatclient.exc import HTTPNotFound
from multiprocessing.dummy import Pool as ThreadPool

from .heat import HeatWrapper
from .models import Stack
from .utils import (UP_STATES, LAUNCH_STATE, SUSPEND_STATE,
                    SUSPEND_ISSUED_STATE, SUSPEND_RETRY_STATE, DELETED_STATE,
                    DELETE_IN_PROGRESS_STATE, DELETE_FAILED_STATE,
                    DELETE_STATE, get_xblock_configuration)


class SuspenderJob(object):
    """
    Suspends stacks.

    """
    settings = {}
    stdout = None

    def __init__(self, settings, stdout):
        self.settings = settings
        self.stdout = stdout

    def run(self):
        """
        Suspend stacks.

        """
        timeout = self.settings.get("suspend_timeout", 120)
        concurrency = self.settings.get("suspend_concurrency", 4)
        timedelta = timezone.timedelta(seconds=timeout)
        cutoff = timezone.now() - timedelta
        states = list(UP_STATES) + [LAUNCH_STATE, SUSPEND_RETRY_STATE]

        # Get stacks to suspend
        with transaction.atomic():
            stacks = Stack.objects.select_for_update().filter(
                suspend_timestamp__isnull=False
            ).filter(
                suspend_timestamp__lt=cutoff
            ).filter(
                status__in=states
            ).order_by('suspend_timestamp')[:concurrency]

            for stack in stacks:
                stack.status = SUSPEND_STATE
                stack.save()

        # Suspend them
        if self.settings.get("suspend_in_parallel", True):
            pool = ThreadPool(concurrency)
            pool.map(self.suspend_stack, stacks)
            pool.close()
            pool.join()
        else:
            for stack in stacks:
                self.suspend_stack(stack)

    def get_heat_client(self, configuration):
        return HeatWrapper(**configuration).get_client()

    def suspend_stack(self, stack):
        """
        Suspend the stack.

        """
        configuration = get_xblock_configuration(self.settings, stack.provider)
        heat_client = self.get_heat_client(configuration)

        self.stdout.write("Initializing stack [%s] suspension." % stack.name)

        try:
            heat_stack = heat_client.stacks.get(stack_id=stack.name)
        except HTTPNotFound:
            heat_status = DELETED_STATE
        else:
            heat_status = heat_stack.stack_status

        if heat_status in UP_STATES:
            self.stdout.write("Suspending stack [%s]." % (stack.name))

            # Suspend stack
            heat_client.actions.suspend(stack_id=stack.name)

            # Save status
            stack.status = SUSPEND_ISSUED_STATE
            stack.save()
        else:
            self.stdout.write("Cannot suspend stack [%s] "
                              "with status [%s]." % (stack.name, heat_status))

            # Schedule for retry, if it makes sense to do so
            if (heat_status != DELETED_STATE and
                    'FAILED' not in heat_status):
                stack.status = SUSPEND_RETRY_STATE
            else:
                stack.status = heat_status

            stack.save()


class UndertakerJob(object):
    """
    Deletes old stacks.

    """
    settings = {}
    stdout = None

    def __init__(self, settings, stdout):
        self.settings = settings
        self.stdout = stdout

    def run(self):
        """
        Delete stacks.

        """
        age = self.settings.get("delete_age", 14)
        if not age:
            return

        timedelta = timezone.timedelta(days=age)
        cutoff = timezone.now() - timedelta
        dont_delete = [DELETE_STATE,
                       DELETED_STATE,
                       DELETE_IN_PROGRESS_STATE]

        # Get stacks to delete
        with transaction.atomic():
            stacks = Stack.objects.select_for_update().filter(
                suspend_timestamp__isnull=False
            ).filter(
                suspend_timestamp__lt=cutoff
            ).exclude(
                status__in=dont_delete
            ).order_by('suspend_timestamp')

            for stack in stacks:
                stack.status = DELETE_STATE
                stack.save()

        # Delete them
        for stack in stacks:
            self.delete_stack(stack)

    def get_heat_client(self, configuration):
        return HeatWrapper(**configuration).get_client()

    def delete_stack(self, stack):
        """
        Delete the stack.

        """
        configuration = get_xblock_configuration(self.settings, stack.provider)
        timeouts = configuration.get('task_timeouts', {})
        sleep = timeouts.get('sleep', 10)
        retries = timeouts.get('retries', 90)
        heat_client = self.get_heat_client(configuration)

        self.stdout.write("Initializing stack [%s] deletion." % stack.name)

        try:
            heat_stack = heat_client.stacks.get(stack_id=stack.name)
        except HTTPNotFound:
            stack.status = DELETED_STATE
            stack.save()
        else:
            self.stdout.write("Deleting stack [%s]." % (stack.name))

            # Delete stack
            heat_client.stacks.delete(stack_id=stack.name)

            # Save status
            stack.status = DELETE_IN_PROGRESS_STATE
            stack.save()

            # Wait until delete finishes.
            retry = 0
            while ('FAILED' not in stack.status and
                   stack.status != DELETED_STATE):
                if retry:
                    time.sleep(sleep)

                try:
                    heat_stack = heat_client.stacks.get(stack_id=stack.name)
                except HTTPNotFound:
                    stack.status = DELETED_STATE
                    stack.save()
                else:
                    stack.status = heat_stack.stack_status
                    stack.save()

                    retry += 1
                    if retry >= retries:
                        self.stdout.write(
                            "Stack [%s], status [%s], took too long to "
                            "delete.  Giving up after %s retries" %
                            (stack.name, stack.status, retry)
                        )
                        stack.status = DELETE_FAILED_STATE
                        stack.save()

            if stack.status == DELETED_STATE:
                self.stdout.write("Stack [%s] deleted successfully." %
                                  stack.name)
