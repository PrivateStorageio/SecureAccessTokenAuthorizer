# Copyright 2019 PrivateStorage.io, LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Tests for ``_zkapauthorizer.lease_maintenance``.
"""

from __future__ import (
    absolute_import,
    unicode_literals,
)

from datetime import (
    datetime,
    timedelta,
)

import attr

from zope.interface import (
    implementer,
)

from testtools import (
    TestCase,
)
from testtools.matchers import (
    Is,
    Equals,
    Always,
    HasLength,
    MatchesAll,
    AllMatch,
    AfterPreprocessing,
)
from testtools.twistedsupport import (
    succeeded,
)
from fixtures import (
    TempDir,
)
from hypothesis import (
    given,
    note,
)
from hypothesis.strategies import (
    builds,
    binary,
    integers,
    lists,
    floats,
    dictionaries,
    randoms,
    composite,
    just,
)

from twisted.python.filepath import (
    FilePath,
)
from twisted.internet.task import (
    Clock,
)
from twisted.internet.defer import (
    succeed,
    maybeDeferred,
)
from twisted.application.service import (
    IService,
)

from allmydata.util.hashutil import (
    CRYPTO_VAL_SIZE,
)
from allmydata.client import (
    SecretHolder,
)
from allmydata.interfaces import (
    IStorageBroker,
    IServer,
)

from ..foolscap import (
    ShareStat,
)

from .matchers import (
    Provides,
    between,
    leases_current,
)
from .strategies import (
    storage_indexes,
    clocks,
    leaf_nodes,
    node_hierarchies,
)

from ..lease_maintenance import (
    NoopMaintenanceObserver,
    MemoryMaintenanceObserver,
    lease_maintenance_service,
    maintain_leases_from_root,
    visit_storage_indexes_from_root,
    renew_leases,
)


def interval_means():
    return floats(
        # It doesn't make sense to have a negative check interval mean.
        min_value=0,
        # We can't make this value too large or it isn't convertable to a
        # timedelta.  Also, even values as large as this one are of
        # questionable value.
        max_value=60 * 60 * 24 * 365,
    ).map(
        # By representing the result as a timedelta we avoid the cases where
        # the lower precision of timedelta compared to float drops the whole
        # value (anything between 0 and 1 microsecond).  This is just one
        # example of how working with timedeltas is nicer, in general.
        lambda s: timedelta(seconds=s),
    )


def dummy_maintain_leases():
    pass


@attr.s
class DummyStorageServer(object):
    """
    A dummy implementation of ``IStorageServer`` from Tahoe-LAFS.

    :ivar dict[bytes, ShareStat] buckets: A mapping from storage index to
        metadata about shares at that storage index.
    """
    clock = attr.ib()
    buckets = attr.ib()
    lease_seed = attr.ib()

    def stat_shares(self, storage_indexes):
        return succeed(list(
            {0: self.buckets[idx]} if idx in self.buckets else {}
            for idx
            in storage_indexes
        ))

    def get_lease_seed(self):
        return self.lease_seed

    def add_lease(self, storage_index, renew_secret, cancel_secret):
        self.buckets[storage_index].lease_expiration = (
            self.clock.seconds() + timedelta(days=31).total_seconds()
        )


class SharesAlreadyExist(Exception):
    pass


def create_shares(storage_server, storage_index, size, lease_expiration):
    """
    Initialize a storage index ("bucket") with some shares.

    :param DummyServer storage_server: The server to populate with shares.
    :param bytes storage_index: The storage index of the shares.
    :param int size: The application data size of the shares.
    :param int lease_expiration: The expiration time for the lease to attach
        to the shares.

    :raise SharesAlreadyExist: If there are already shares at the given
        storage index.

    :return: ``None``
    """
    if storage_index in storage_server.buckets:
        raise SharesAlreadyExist(
            "Cannot create shares for storage index where they already exist.",
        )
    storage_server.buckets[storage_index] = ShareStat(
        size=size,
        lease_expiration=lease_expiration,
    )


def lease_seeds():
    return binary(
        min_size=20,
        max_size=20,
    )


def share_stats():
    return builds(
        ShareStat,
        size=integers(min_value=0),
        lease_expiration=integers(min_value=0, max_value=2 ** 31),
    )


def storage_servers(clocks):
    return builds(
        DummyStorageServer,
        clocks,
        dictionaries(storage_indexes(), share_stats()),
        lease_seeds(),
    ).map(
        DummyServer,
    )


@implementer(IServer)
@attr.s
class DummyServer(object):
    """
    A partial implementation of a Tahoe-LAFS "native" storage server.
    """
    _storage_server = attr.ib()

    def get_storage_server(self):
        return self._storage_server


@implementer(IStorageBroker)
@attr.s
class DummyStorageBroker(object):
    """
    A partial implementation of a Tahoe-LAFS storage broker.
    """
    clock = attr.ib()
    _storage_servers = attr.ib()

    def get_connected_servers(self):
        return self._storage_servers


@composite
def storage_brokers(draw, clocks):
    clock = draw(clocks)
    return DummyStorageBroker(
        clock,
        draw(lists(storage_servers(just(clock)))),
    )


class LeaseMaintenanceServiceTests(TestCase):
    """
    Tests for the service returned by ``lease_maintenance_service``.
    """
    @given(randoms())
    def test_interface(self, random):
        """
        The service provides ``IService``.
        """
        clock = Clock()
        service = lease_maintenance_service(
            dummy_maintain_leases,
            clock,
            FilePath(self.useFixture(TempDir()).join(u"last-run")),
            random,
        )
        self.assertThat(
            service,
            Provides([IService]),
        )

    @given(
        randoms(),
        interval_means(),
    )
    def test_initial_interval(self, random, mean):
        """
        When constructed without a value for ``last_run``,
        ``lease_maintenance_service`` schedules its first run to take place
        after an interval that falls uniformly in range centered on ``mean``
        with a size of ``range``.
        """
        clock = Clock()
        # Construct a range that fits in with the mean
        range_ = timedelta(
            seconds=random.uniform(0, mean.total_seconds()),
        )

        service = lease_maintenance_service(
            dummy_maintain_leases,
            clock,
            FilePath(self.useFixture(TempDir()).join(u"last-run")),
            random,
            mean,
            range_,
        )
        service.startService()
        [maintenance_call] = clock.getDelayedCalls()

        datetime_now = datetime.utcfromtimestamp(clock.seconds())
        low = datetime_now + mean - (range_ / 2)
        high = datetime_now + mean + (range_ / 2)
        self.assertThat(
            datetime.utcfromtimestamp(maintenance_call.getTime()),
            between(low, high),
        )

    @given(
        randoms(),
        clocks(),
        interval_means(),
        interval_means(),
    )
    def test_initial_interval_with_last_run(self, random, clock, mean, since_last_run):
        """
        When constructed with a value for ``last_run``,
        ``lease_maintenance_service`` schedules its first run to take place
        sooner than it otherwise would, by at most the time since the last
        run.
        """
        datetime_now = datetime.utcfromtimestamp(clock.seconds())
        # Construct a range that fits in with the mean
        range_ = timedelta(
            seconds=random.uniform(0, mean.total_seconds()),
        )

        # Figure out the absolute last run time.
        last_run = datetime_now - since_last_run
        last_run_path = FilePath(self.useFixture(TempDir()).join(u"last-run"))
        last_run_path.setContent(last_run.isoformat())

        service = lease_maintenance_service(
            dummy_maintain_leases,
            clock,
            last_run_path,
            random,
            mean,
            range_,
        )
        service.startService()
        [maintenance_call] = clock.getDelayedCalls()

        low = datetime_now + max(
            timedelta(0),
            mean - (range_ / 2) - since_last_run,
        )
        high = max(
            # If since_last_run is one microsecond (precision of timedelta)
            # then the range is indivisible.  Avoid putting the expected high
            # below the expected low.
            low,
            datetime_now + mean + (range_ / 2) - since_last_run,
        )

        note("mean: {}\nrange: {}\nnow: {}\nlow: {}\nhigh: {}\nsince last: {}".format(
            mean, range_, datetime_now, low, high, since_last_run,
        ))

        self.assertThat(
            datetime.utcfromtimestamp(maintenance_call.getTime()),
            between(low, high),
        )

    @given(
        randoms(),
        clocks(),
    )
    def test_clean_up_when_stopped(self, random, clock):
        """
        When the service is stopped, the delayed call in the reactor is removed.
        """
        service = lease_maintenance_service(
            lambda: None,
            clock,
            FilePath(self.useFixture(TempDir()).join(u"last-run")),
            random,
        )
        service.startService()
        self.assertThat(
            maybeDeferred(service.stopService),
            succeeded(Is(None)),
        )
        self.assertThat(
            clock.getDelayedCalls(),
            Equals([]),
        )
        self.assertThat(
            service.running,
            Equals(False),
        )

    @given(
        randoms(),
        clocks(),
    )
    def test_nodes_visited(self, random, clock):
        """
        When the service runs, it calls the ``maintain_leases`` object.
        """
        leases_maintained_at = []
        def maintain_leases():
            leases_maintained_at.append(datetime.utcfromtimestamp(clock.seconds()))

        service = lease_maintenance_service(
            maintain_leases,
            clock,
            FilePath(self.useFixture(TempDir()).join(u"last-run")),
            random,
        )
        service.startService()
        [maintenance_call] = clock.getDelayedCalls()
        clock.advance(maintenance_call.getTime() - clock.seconds())

        self.assertThat(
            leases_maintained_at,
            Equals([datetime.utcfromtimestamp(clock.seconds())]),
        )


class VisitStorageIndexesFromRootTests(TestCase):
    """
    Tests for ``visit_storage_indexes_from_root``.
    """
    @given(node_hierarchies(), clocks())
    def test_visits_all_nodes(self, root_node, clock):
        """
        The operation calls the specified visitor with every node from the root to
        its deepest children.
        """
        visited = []
        def perform_visit(visit_assets):
            return visit_assets(visited.append)

        operation = visit_storage_indexes_from_root(
            perform_visit,
            lambda: [root_node],
        )

        self.assertThat(
            operation(),
            succeeded(Always()),
        )
        expected = root_node.flatten()
        self.assertThat(
            visited,
            MatchesAll(
                HasLength(len(expected)),
                AfterPreprocessing(
                    set,
                    Equals(set(
                        node.get_storage_index()
                        for node
                        in expected
                    )),
                ),
            ),
        )


class RenewLeasesTests(TestCase):
    """
    Tests for ``renew_leases``.
    """
    @given(storage_brokers(clocks()), lists(leaf_nodes(), unique=True))
    def test_renewed(self, storage_broker, nodes):
        """
        ``renew_leases`` renews the leases of shares on all storage servers which
        have no more than the specified amount of time remaining on their
        current lease.
        """
        lease_secret = b"\0" * CRYPTO_VAL_SIZE
        convergence_secret = b"\1" * CRYPTO_VAL_SIZE
        secret_holder = SecretHolder(lease_secret, convergence_secret)
        min_lease_remaining = timedelta(days=3)

        # Make sure that the storage brokers have shares at the storage
        # indexes we're going to operate on.
        for storage_server in storage_broker.get_connected_servers():
            for node in nodes:
                try:
                    create_shares(
                        storage_server.get_storage_server(),
                        node.get_storage_index(),
                        size=123,
                        lease_expiration=int(storage_broker.clock.seconds()),
                    )
                except SharesAlreadyExist:
                    # If Hypothesis already put some shares in this storage
                    # index, that's okay too.
                    pass

        def get_now():
            return datetime.utcfromtimestamp(
                storage_broker.clock.seconds(),
            )

        def visit_assets(visit):
            for node in nodes:
                visit(node.get_storage_index())
            return succeed(None)

        d = renew_leases(
            visit_assets,
            storage_broker,
            secret_holder,
            min_lease_remaining,
            NoopMaintenanceObserver,
            get_now,
        )
        self.assertThat(
            d,
            succeeded(Always()),
        )

        relevant_storage_indexes = set(
            node.get_storage_index()
            for node
            in nodes
        )

        self.assertThat(
            list(
                server.get_storage_server()
                for server
                in storage_broker.get_connected_servers()
            ),
            AllMatch(leases_current(
                relevant_storage_indexes,
                get_now(),
                min_lease_remaining,
            )),
        )


class MaintainLeasesFromRootTests(TestCase):
    """
    Tests for ``maintain_leases_from_root``.
    """
    @given(storage_brokers(clocks()), node_hierarchies())
    def test_renewed(self, storage_broker, root_node):
        """
        ``maintain_leases_from_root`` creates an operation which renews the leases
        of shares on all storage servers which have no more than the specified
        amount of time remaining on their current lease.
        """
        lease_secret = b"\0" * CRYPTO_VAL_SIZE
        convergence_secret = b"\1" * CRYPTO_VAL_SIZE
        secret_holder = SecretHolder(lease_secret, convergence_secret)
        min_lease_remaining = timedelta(days=3)

        def get_now():
            return datetime.utcfromtimestamp(
                storage_broker.clock.seconds(),
            )

        operation = maintain_leases_from_root(
            lambda: [root_node],
            storage_broker,
            secret_holder,
            min_lease_remaining,
            NoopMaintenanceObserver,
            get_now,
        )
        d = operation()
        self.assertThat(
            d,
            succeeded(Always()),
        )

        relevant_storage_indexes = set(
            node.get_storage_index()
            for node
            in root_node.flatten()
        )

        self.assertThat(
            list(
                server.get_storage_server()
                for server
                in storage_broker.get_connected_servers()
            ),
            AllMatch(leases_current(
                relevant_storage_indexes,
                get_now(),
                min_lease_remaining,
            ))
        )

    @given(storage_brokers(clocks()), node_hierarchies())
    def test_activity_observed(self, storage_broker, root_node):
        """
        ``maintain_leases_from_root`` creates an operation which uses the given
        activity observer to report its progress.
        """
        lease_secret = b"\0" * CRYPTO_VAL_SIZE
        convergence_secret = b"\1" * CRYPTO_VAL_SIZE
        secret_holder = SecretHolder(lease_secret, convergence_secret)
        min_lease_remaining = timedelta(days=3)

        def get_now():
            return datetime.utcfromtimestamp(
                storage_broker.clock.seconds(),
            )

        observer = MemoryMaintenanceObserver()
        # There is only one available.
        observers = [observer]
        progress = observers.pop
        operation = maintain_leases_from_root(
            lambda: [root_node],
            storage_broker,
            secret_holder,
            min_lease_remaining,
            progress,
            get_now,
        )
        d = operation()
        self.assertThat(
            d,
            succeeded(Always()),
        )

        expected = []
        for node in root_node.flatten():
            for server in storage_broker.get_connected_servers():
                try:
                    stat = server.get_storage_server().buckets[node.get_storage_index()]
                except KeyError:
                    continue
                else:
                    # DummyStorageServer always pretends to have only one share
                    expected.append([stat.size])

        # The visit order doesn't matter.
        expected.sort()

        self.assertThat(
            observer.observed,
            AfterPreprocessing(
                sorted,
                Equals(expected),
            ),
        )
