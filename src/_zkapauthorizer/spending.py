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
A module for logic controlling the manner in which ZKAPs are spent.
"""

from __future__ import annotations

from collections.abc import Container
from typing import Callable

import attr
from zope.interface import Attribute, Interface, implementer

from ._attrs_zope import provides
from .eliot import GET_PASSES, INVALID_PASSES, RESET_PASSES, SPENT_PASSES
from .model import Pass, UnblindedToken, VoucherStore


class IPassGroup(Interface):
    """
    A group of passed meant to be spent together.
    """

    unblinded_tokens: list[UnblindedToken] = Attribute(
        "The unblinded signatures used to create the passes."
    )
    passes: list[Pass] = Attribute("The passes themselves.")

    def split(select_indices: Container[int]) -> tuple[IPassGroup, IPassGroup]:
        """
        Create two new ``IPassGroup`` providers.  The first contains all
        passes in this group at the given indices.  The second contains all
        the others.

        :param select_indices: The indices of the passes to include in the
            first resulting group.

        :return: The two new groups.
        """

    def expand(by_amount: int) -> IPassGroup:
        """
        Create a new ``IPassGroup`` provider which contains all of this
        group's passes and some more.

        :param by_amount: The number of additional passes the resulting group
            should contain.

        :return: The new group.
        """

    def mark_spent() -> None:
        """
        The passes have been spent successfully.  Ensure none of them appear in
        any ``IPassGroup`` provider created in the future.
        """

    def mark_invalid(reason: str) -> None:
        """
        The passes could not be spent.  Ensure none of them appear in any
        ``IPassGroup`` provider created in the future.

        :param reason: A short description of the reason the passes could not
            be spent.
        """

    def reset() -> None:
        """
        The passes have not been spent.  Return them to for use in a future
        ``IPassGroup`` provider.

        :return: ``None``
        """


class IPassFactory(Interface):
    """
    An object which can create passes.
    """

    def get(message: bytes, num_passes: int) -> IPassGroup:
        """
        :param message: A request-binding message for the resulting passes.

        :param num_passes: The number of passes to request.

        :return: A group of passes bound to the given message and of the
            requested size.
        """

    def mark_spent(unblinded_tokens: list[UnblindedToken]) -> None:
        """
        See ``IPassGroup.mark_spent``
        """

    def mark_invalid(reason: str, unblinded_tokens: list[UnblindedToken]) -> None:
        """
        See ``IPassGroup.mark_invalid``
        """

    def reset(unblinded_tokens: list[UnblindedToken]) -> None:
        """
        See ``IPassGroup.reset``
        """


@implementer(IPassGroup)
@attr.s
class PassGroup(object):
    """
    Track the state of a group of passes intended as payment for an operation.

    :ivar _message: The request binding message for this group of
        passes.

    :ivar IPassFactory _factory: The factory which created this pass group.

    :ivar list[Pass] passes: The passes of which this group consists.
    """

    _message: bytes = attr.ib(validator=attr.validators.instance_of(bytes))
    _factory: IPassFactory = attr.ib(validator=provides(IPassFactory))
    _tokens: list[tuple[UnblindedToken, Pass]] = attr.ib(
        validator=attr.validators.instance_of(list)
    )

    @property
    def passes(self) -> list[Pass]:
        return list(pass_ for (unblinded_token, pass_) in self._tokens)

    @property
    def unblinded_tokens(self) -> list[UnblindedToken]:
        return list(unblinded_token for (unblinded_token, pass_) in self._tokens)

    def split(self, select_indices: Container[int]) -> tuple[PassGroup, PassGroup]:
        selected = []
        unselected = []
        for idx, t in enumerate(self._tokens):
            if idx in select_indices:
                selected.append(t)
            else:
                unselected.append(t)
        return (
            attr.evolve(self, tokens=selected),
            attr.evolve(self, tokens=unselected),
        )

    def expand(self, by_amount: int) -> PassGroup:
        return self + self._factory.get(self._message, by_amount)

    def __add__(self, other: IPassGroup) -> PassGroup:
        return attr.evolve(
            self,
            tokens=self._tokens + list(zip(other.unblinded_tokens, other.passes)),
        )

    def mark_spent(self) -> None:
        self._factory.mark_spent(self.unblinded_tokens)

    def mark_invalid(self, reason: str) -> None:
        self._factory.mark_invalid(reason, self.unblinded_tokens)

    def reset(self) -> None:
        self._factory.reset(self.unblinded_tokens)


@implementer(IPassFactory)
@attr.s
class SpendingController(object):
    """
    A ``SpendingController`` gives out ZKAPs and arranges for re-spend
    attempts when necessary.
    """

    get_unblinded_tokens: Callable[[int], list[UnblindedToken]] = attr.ib()
    discard_unblinded_tokens: Callable[[list[UnblindedToken]], None] = attr.ib()
    invalidate_unblinded_tokens: Callable[[str, list[UnblindedToken]], None] = attr.ib()
    reset_unblinded_tokens: Callable[[list[UnblindedToken]], None] = attr.ib()

    tokens_to_passes: Callable[[bytes, list[UnblindedToken]], list[Pass]] = attr.ib()

    @classmethod
    def for_store(
        cls,
        tokens_to_passes: Callable[[bytes, list[UnblindedToken]], list[Pass]],
        store: VoucherStore,
    ) -> "SpendingController":
        return cls(
            get_unblinded_tokens=store.get_unblinded_tokens,
            discard_unblinded_tokens=store.discard_unblinded_tokens,
            invalidate_unblinded_tokens=store.invalidate_unblinded_tokens,
            reset_unblinded_tokens=store.reset_unblinded_tokens,
            tokens_to_passes=tokens_to_passes,
        )

    def get(self, message: bytes, num_passes: int) -> PassGroup:
        unblinded_tokens = self.get_unblinded_tokens(num_passes)
        passes = self.tokens_to_passes(message, unblinded_tokens)
        GET_PASSES.log(
            message=message.decode("utf-8"),
            count=num_passes,
        )
        return PassGroup(message, self, list(zip(unblinded_tokens, passes)))

    def mark_spent(self, unblinded_tokens: list[UnblindedToken]) -> None:
        SPENT_PASSES.log(
            count=len(unblinded_tokens),
        )
        self.discard_unblinded_tokens(unblinded_tokens)

    def mark_invalid(self, reason: str, unblinded_tokens: list[UnblindedToken]) -> None:
        INVALID_PASSES.log(
            reason=reason,
            count=len(unblinded_tokens),
        )
        self.invalidate_unblinded_tokens(reason, unblinded_tokens)

    def reset(self, unblinded_tokens: list[UnblindedToken]) -> None:
        RESET_PASSES.log(
            count=len(unblinded_tokens),
        )
        self.reset_unblinded_tokens(unblinded_tokens)
