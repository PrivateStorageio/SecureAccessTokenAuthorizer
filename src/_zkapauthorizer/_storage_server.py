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
A Tahoe-LAFS RIStorageServer-alike which authorizes writes and lease
updates using per-call passes.

This is the server part of a storage access protocol.  The client part is
implemented in ``_storage_client.py``.
"""

from __future__ import (
    absolute_import,
)

from functools import (
    partial,
)

import attr
from attr.validators import (
    provides,
    instance_of,
)

from zope.interface import (
    implementer_only,
)
from foolscap.api import (
    Referenceable,
)
from foolscap.ipb import (
    IReferenceable,
    IRemotelyCallable,
)
from allmydata.interfaces import (
    RIStorageServer,
)
from privacypass import (
    TokenPreimage,
    VerificationSignature,
    SigningKey,
)

from twisted.python.reflect import (
    namedAny,
)
from twisted.internet.interfaces import (
    IReactorTime,
)

from .foolscap import (
    RIPrivacyPassAuthorizedStorageServer,
)
from .storage_common import (
    BYTES_PER_PASS,
    required_passes,
    allocate_buckets_message,
    add_lease_message,
    renew_lease_message,
    slot_testv_and_readv_and_writev_message,
    has_writes,
)

class MorePassesRequired(Exception):
    """
    Storage operations fail with ``MorePassesRequired`` when they are not
    accompanied by a sufficient number of valid passes.

    :ivar int valid_count: The number of valid passes presented in the
        operation.

    ivar int required_count: The number of valid passes which must be
        presented for the operation to be authorized.
    """
    def __init__(self, valid_count, required_count):
        self.valid_count = valid_count
        self.required_count = required_count

    def __repr__(self):
        return "MorePassedRequired(valid_count={}, required_count={})".format(
            self.valid_count,
            self.required_count,
        )

    def __str__(self):
        return repr(self)


class LeaseRenewalRequired(Exception):
    """
    Mutable write operations fail with ``LeaseRenewalRequired`` when the slot
    which is the target of the write does not have an active lease and no
    passes are supplied to create one.
    """


@implementer_only(RIPrivacyPassAuthorizedStorageServer, IReferenceable, IRemotelyCallable)
# It would be great to use `frozen=True` (value-based hashing) instead of
# `cmp=False` (identity based hashing) but Referenceable wants to set some
# attributes on self and it's hard to avoid that.
@attr.s(cmp=False)
class ZKAPAuthorizerStorageServer(Referenceable):
    """
    A class which wraps an ``RIStorageServer`` to insert pass validity checks
    before allowing certain functionality.
    """
    _original = attr.ib(validator=provides(RIStorageServer))
    _signing_key = attr.ib(validator=instance_of(SigningKey))
    _clock = attr.ib(
        validator=provides(IReactorTime),
        default=attr.Factory(partial(namedAny, "twisted.internet.reactor")),
    )

    def _is_invalid_pass(self, message, pass_):
        """
        Cryptographically check the validity of a single pass.

        :param unicode message: The shared message for pass validation.
        :param bytes pass_: The encoded pass to validate.

        :return bool: ``False`` (invalid) if the pass includes a valid
            signature, ``True`` (valid) otherwise.
        """
        assert isinstance(message, unicode), "message %r not unicode" % (message,)
        assert isinstance(pass_, bytes), "pass %r not bytes" % (pass_,)
        try:
            preimage_base64, signature_base64 = pass_.split(b" ")
            preimage = TokenPreimage.decode_base64(preimage_base64)
            proposed_signature = VerificationSignature.decode_base64(signature_base64)
            unblinded_token = self._signing_key.rederive_unblinded_token(preimage)
            verification_key = unblinded_token.derive_verification_key_sha512()
            invalid_pass = verification_key.invalid_sha512(proposed_signature, message.encode("utf-8"))
            return invalid_pass
        except Exception:
            # It would be pretty nice to log something here, sometimes, I guess?
            return True

    def _validate_passes(self, message, passes):
        """
        Check all of the given passes for validity.

        :param unicode message: The shared message for pass validation.
        :param list[bytes] passes: The encoded passes to validate.

        :return list[bytes]: The passes which are found to be valid.
        """
        return list(
            pass_
            for pass_
            in passes
            if not self._is_invalid_pass(message, pass_)
        )

    def remote_get_version(self):
        """
        Pass-through without pass check to allow clients to learn about our
        version and configuration in case it helps them decide how to behave.
        """
        return self._original.remote_get_version()

    def remote_allocate_buckets(self, passes, storage_index, renew_secret, cancel_secret, sharenums, allocated_size, canary):
        """
        Pass-through after a pass check to ensure that clients can only allocate
        storage for immutable shares if they present valid passes.
        """
        valid_passes = self._validate_passes(
            allocate_buckets_message(storage_index),
            passes,
        )
        check_pass_quantity_for_write(len(valid_passes), sharenums, allocated_size)

        return self._original.remote_allocate_buckets(
            storage_index,
            renew_secret,
            cancel_secret,
            sharenums,
            allocated_size,
            canary,
        )

    def remote_get_buckets(self, storage_index):
        """
        Pass-through without pass check to let clients read immutable shares as
        long as those shares exist.
        """
        return self._original.remote_get_buckets(storage_index)

    def remote_add_lease(self, passes, storage_index, *a, **kw):
        """
        Pass-through after a pass check to ensure clients can only extend the
        duration of share storage if they present valid passes.
        """
        self._validate_passes(add_lease_message(storage_index), passes)
        return self._original.remote_add_lease(storage_index, *a, **kw)

    def remote_renew_lease(self, passes, storage_index, *a, **kw):
        """
        Pass-through after a pass check to ensure clients can only extend the
        duration of share storage if they present valid passes.
        """
        self._validate_passes(renew_lease_message(storage_index), passes)
        return self._original.remote_renew_lease(storage_index, *a, **kw)

    def remote_advise_corrupt_share(self, *a, **kw):
        """
        Pass-through without a pass check to let clients inform us of possible
        issues with the system without incurring any cost to themselves.
        """
        return self._original.remote_advise_corrupt_share(*a, **kw)

    def remote_slot_testv_and_readv_and_writev(
            self,
            passes,
            storage_index,
            secrets,
            tw_vectors,
            r_vector,
    ):
        """
        Pass-through after a pass check to ensure clients can only allocate
        storage for mutable shares if they present valid passes.

        :note: This method can be used both to allocate storage and to rewrite
            data in already-allocated storage.  These cases may not be the
            same from the perspective of pass validation.
        """
        renew_leases = False

        if has_writes(tw_vectors):
            # Writes are allowed to shares with active leases.
            if not has_active_lease(
                self._original,
                storage_index,
                self._clock.seconds(),
            ):
                # Passes may be supplied with the write to create the
                # necessary lease as part of the same operation.  This must be
                # supported because there is no separate protocol action to
                # *create* a slot.  Clients just begin writing to it.
                valid_passes = self._validate_passes(
                    slot_testv_and_readv_and_writev_message(storage_index),
                    passes,
                )
                check_pass_quantity_for_mutable_write(len(valid_passes), tw_vectors)
                renew_leases = True

        # Skip over the remotely exposed method and jump to the underlying
        # implementation which accepts one additional parameter that we know
        # about (and don't expose over the network): renew_leases.  We always
        # pass False for this because we want to manage leases completely
        # separately from writes.
        return self._original.slot_testv_and_readv_and_writev(
            storage_index,
            secrets,
            tw_vectors,
            r_vector,
            renew_leases=renew_leases,
        )

    def remote_slot_readv(self, *a, **kw):
        """
        Pass-through without a pass check to let clients read mutable shares as
        long as those shares exist.
        """
        return self._original.remote_slot_readv(*a, **kw)


def get_sharenums(tw_vectors):
    """
    :param tw_vectors: See
        ``allmydata.interfaces.TestAndWriteVectorsForShares``.

    :return set[int]: The share numbers which the given test/write vectors would write to.
    """
    return set(
        sharenum
        for (sharenum, (test, data, new_length))
        in tw_vectors.items()
        if data
    )


def get_allocated_size(tw_vectors):
    """
    :param tw_vectors: See
        ``allmydata.interfaces.TestAndWriteVectorsForShares``.

    :return int: The largest position ``tw_vectors`` writes in any share.
    """
    return max(
        list(
            max(offset + len(s) for (offset, s) in data)
            for (sharenum, (test, data, new_length))
            in tw_vectors.items()
            if data
        ),
    )


def has_active_lease(storage_server, storage_index, now):
    """
    :param allmydata.storage.server.StorageServer storage_server: A storage
        server to use to look up lease information.

    :param bytes storage_index: A storage index to use to look up lease
        information.

    :param float now: The current time as a POSIX timestamp.

    :return bool: ``True`` if any only if the given storage index has a lease
        with an expiration time after ``now``.
    """
    leases = storage_server.get_slot_leases(storage_index)
    return any(
        lease.get_expiration_time() > now
        for lease
        in leases
    )


def check_pass_quantity_for_write(valid_count, sharenums, allocated_size):
    """
    Determine if the given number of valid passes is sufficient for an
    attempted write.

    :param int valid_count: The number of valid passes to consider.
    :param set[int] sharenums: The shares being written to.
    :param int allocated_size: The size of each share.

    :raise MorePassedRequired: If the number of valid passes given is too
        small.

    :return: ``None`` if the number of valid passes given is sufficient.
    """
    required_pass_count = required_passes(BYTES_PER_PASS, sharenums, allocated_size)
    if valid_count < required_pass_count:
        raise MorePassesRequired(
            valid_count,
            required_pass_count,
        )


def check_pass_quantity_for_mutable_write(valid_count, tw_vectors):
    """
    Determine if the given number of valid passes is sufficient for an
    attempted write to a slot.

    :param int valid_count: The number of valid passes to consider.

    :param tw_vectors: See
        ``allmydata.interfaces.TestAndWriteVectorsForShares``.
    """
    sharenums = get_sharenums(tw_vectors)
    allocated_size = get_allocated_size(tw_vectors)
    check_pass_quantity_for_write(valid_count, sharenums, allocated_size)


# I don't understand why this is required.
# ZKAPAuthorizerStorageServer is-a Referenceable.  It seems like
# the built in adapter should take care of this case.
from twisted.python.components import (
    registerAdapter,
)
from foolscap.referenceable import (
    ReferenceableSlicer,
)
from foolscap.ipb import (
    ISlicer,
)
registerAdapter(ReferenceableSlicer, ZKAPAuthorizerStorageServer, ISlicer)
