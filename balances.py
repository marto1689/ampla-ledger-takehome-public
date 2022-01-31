#!/usr/bin/env python3
import sqlite3
from datetime import datetime, date, timedelta
from decimal import Decimal
from typing import List


class BalanceEvent:
    """
    Class that models/represents and store the information about event records.
    An instance of this class holds an event information needed to perform balance statistics calculation.
    Properties:
       - event_id:     the database id for instance event
       - ipb:          interest payable balance amount that has accrued and has yet to be paid associated to this event
       - current_amt:  the current balance amount of instance event
       - original_amt: the initial balance amount of instance event
       - event_date:   the date when event happens
       - event_type:   the event type, supported types: "advance" and "payment"
    """
    def __init__(self, event_id: int,
                 interest_payable_balance: Decimal = Decimal(0),
                 current_amt: Decimal = Decimal(0),
                 original_amt: Decimal = Decimal(0),
                 event_date: date = None,
                 event_type: str = None):

        self.event_id = event_id
        self.ipb = interest_payable_balance
        self.current_amt = current_amt
        self.original_amt = original_amt
        self.event_date = event_date
        self.event_type = event_type


class BalanceStatisticsCalculator:
    """
    Class in charge of perform the balance events statistics calculations and preserve this result.

    Usage for this class is:
        - BalanceStatisticsCalculator(db_path="...", end_date="...").calculate_balance_statistics()

    Once calculate_balance_statistics() ran the balance statistics results will be found in following internal vars:
        - self._overall_advance_balance: Aggregate Advance Balance
        - self._overall_interest_payable_balance: Total Interest Payable Balance
        - self._overall_interest_paid: Total Interest Paid
        - self._overall_payments_for_future: Balance Applicable to Future Advances
        - self._advance_event[x] and self._payment_event[x]:
            - current_amt:  the final event balance amount after statistics calculation
            - original_amt: the initial event balance amount
            - ipb:          the amount of interest that has accrued and has yet to be paid associated to this event
            - event_date:   the date when event happens
    """
    # constants of the class
    ACCRUED_INTEREST_CONSTANT = Decimal(0.00035)
    DATE_FORMAT = "%Y-%m-%d"
    ADVANCE = "advance"
    PAYMENT = "payment"

    def __init__(self, db_path: str, end_date: str = str(datetime.now().date())):
        """
        Initializes the BalanceStatisticsCalculator. Optionally you can pass end_date to limit event records
        to work with during statistics calculation.
        :param db_path:   database connection path.
        :param end_date:  upper limit date of the event records to work with. default: today's date
        """
        # init the end date to filter event records with the min date between today and given end date param.
        self._end_date = min(datetime.now().date(), datetime.strptime(end_date, self.DATE_FORMAT).date())
        self._db_path = db_path

        # init variables where will preserve overall the balance statistics calculation
        self._overall_advance_balance = Decimal(0)
        self._overall_interest_payable_balance = Decimal(0)
        self._overall_interest_paid = Decimal(0)
        self._overall_payments_for_future = Decimal(0)

        # internal variables to perform balance event statistics
        # self._advance_events will contain all balance advance events sort by its creation date
        self._advance_events = []
        # self._advance_events will contain all balance payment events sort by its creation date
        self._payment_events = []

        # dict that will save by date the oldest advance event with minor event id within the events with same date.
        #  - key: advance event date.
        #  - value: the oldest advance event object reference for that date.
        # it will be used to access fast (O(1)) to advance event by date and assign/query its interest payable balance.
        self._old_adv_event_by_date_dict = {}

        # self._old_active_adv_event: it will contain the reference object to the oldest active advance event:
        # it means the advance event which the oldest date and current amount and/or interest payable balances
        # still to be paid.
        self._old_active_adv_event = None

        # self._old_active_ipb_adv_event: it will contain the reference object to the oldest active advance
        # event with interest payable to be paid.
        self._old_active_ipb_adv_event = None

        # self._old_active_pay_event: it will contain the reference object to the oldest active payment
        # event, it means the payment event which the oldest date and current amount greater than zero.
        self._old_active_pay_event = None

    def calculate_balance_statistics(self):
        """
        Main method of BalanceStatisticsCalculator in charge to perform the balance statistics calculation over
        event records get from given database path and limit by specified self._end_date.
        """
        # init internal class variables and define auxiliary ones that help to perform statistics calculations.
        self._init_internal_variables()
        adv_id = 0              # current advance event id
        pay_id = 0              # current payment event id
        old_active_adv_id = 0   # id to the oldest active advance event in self._advance_event
        old_active_pay_id = 0   # id to the oldest active payment event in self._payment_event

        # Balance Statistics calculations, main loop there are 2 scenarios:
        # 1) The event to process is PAYMENT: following "Payment Calculations" in documentation apply following steps:
        #    a) first try to reduce as much as possible the "interest payable balance" for those active advance events
        #       with ipb to still be paid, and they are in past date than the payment event.
        #    b) second try to reduce as much as possible the advance event's current amount that still be paid, and
        #       they are in past date than the payment event.
        #    In both scenarios a) and b) do this while payment event has money, starting by the oldest active advance
        #    and so on.
        # 2) The event to process is ADVANCE:
        #    - Starting from the oldest active payment and using all active payment events that are in past or same
        #      date than the advance event reduce as much as possible the advance event's amount.
        # In both scenarios 1) and 2) after payment/advance process ends recalculate the new "interest payable balance"
        # that new sum of all advance balances debt will generate in future days.
        # Formula:
        #     event's ipb = 0.00035 x "sum of all advance balances" x days between event's date and next event date.
        # During the loops we'll update "self._overall_xxxx", place where we'll find final balance statistics results.
        to_process_event = self._get_next_event_to_process(adv_id=adv_id, pay_id=pay_id)
        while to_process_event is not None:

            # PAYMENT case
            if to_process_event.event_type == self.PAYMENT:
                # loop to cancel active interest payable balances while payment event has money, details explained in a)
                old_active_ipb_adv_id = old_active_adv_id
                while old_active_ipb_adv_id < len(self._advance_events) and \
                        to_process_event.current_amt > 0 and \
                        to_process_event.event_date > self._advance_events[old_active_ipb_adv_id].event_date:

                    # get reference of the oldest active advance with ipb to pay to simplify code below
                    self._old_active_ipb_adv_event = self._advance_events[old_active_ipb_adv_id]

                    if to_process_event.current_amt >= self._old_active_ipb_adv_event.ipb:
                        self._overall_interest_paid += self._old_active_ipb_adv_event.ipb
                        self._overall_interest_payable_balance -= self._old_active_ipb_adv_event.ipb
                        to_process_event.current_amt -= self._old_active_ipb_adv_event.ipb
                        self._old_active_ipb_adv_event.ipb = Decimal(0)
                    else:
                        self._overall_interest_paid += to_process_event.current_amt
                        self._overall_interest_payable_balance -= to_process_event.current_amt
                        self._old_active_ipb_adv_event.ipb -= to_process_event.current_amt
                        to_process_event.current_amt = Decimal(0)

                    # when advance ipb are totally debt, move id to the next "new" oldest advance with ipb to be paid,
                    # otherwise it was consumed all payment amount, then move id to the next "new" oldest payment.
                    if self._old_active_ipb_adv_event.ipb == 0:
                        old_active_ipb_adv_id += 1
                    else:
                        old_active_pay_id += 1

                # loop to cancel active advances debt while payment event has money, details explained in b)
                while old_active_adv_id < len(self._advance_events) and \
                        to_process_event.current_amt > 0 and \
                        to_process_event.event_date > self._advance_events[old_active_adv_id].event_date:

                    # get and preserve the oldest active advance balance event reference for future interest calculation
                    self._old_active_adv_event = self._advance_events[old_active_adv_id]

                    # try to cancel the current advance amount debt
                    if to_process_event.current_amt > self._old_active_adv_event.current_amt:
                        self._overall_advance_balance -= self._old_active_adv_event.current_amt
                        to_process_event.current_amt -= self._old_active_adv_event.current_amt
                        self._old_active_adv_event.current_amt = Decimal(0)
                    else:
                        self._overall_advance_balance -= to_process_event.current_amt
                        self._old_active_adv_event.current_amt -= to_process_event.current_amt
                        to_process_event.current_amt = Decimal(0)

                    # when oldest advance amount and its interest are totally debt move id to the next "new" oldest
                    # advance to be paid, otherwise it was consumed all payment amount then move id to the next
                    # "new" oldest payment.
                    if self._old_active_adv_event.current_amt == 0 and self._old_active_adv_event.ipb == 0:
                        old_active_adv_id += 1
                    else:
                        old_active_pay_id += 1

                # calculate the new interest payable balance that current debt will generate in future days.
                next_event_date = self._get_next_event_date(adv_id=adv_id, pay_id=pay_id + 1)
                self._old_active_adv_event.ipb = Decimal((next_event_date - to_process_event.event_date).days) *\
                    Decimal(self.ACCRUED_INTEREST_CONSTANT * self._overall_advance_balance)
                self._overall_interest_payable_balance += self._old_active_adv_event.ipb

                # move current payment id to the next payment event to be processed in the list.
                pay_id += 1

            # ADVANCE case
            if to_process_event.event_type == self.ADVANCE:
                # loop to cancel the debt and interests in current advance while payment events in the past
                # has money for, details explained in 2).
                while old_active_pay_id < len(self._payment_events) and \
                        to_process_event.event_date >= self._payment_events[old_active_pay_id].event_date and \
                        to_process_event.current_amt > 0 and self._payment_events[old_active_pay_id].current_amt > 0:

                    # get the oldest payment event reference to simplify the read of code below.
                    self._old_active_pay_event = self._payment_events[old_active_pay_id]

                    #  try to reduce current advance amount with the oldest active payment
                    if to_process_event.current_amt > self._old_active_pay_event.current_amt:
                        to_process_event.current_amt -= self._old_active_pay_event.current_amt
                        self._old_active_pay_event.current_amt = Decimal(0)
                    else:
                        self._old_active_pay_event.current_amt -= to_process_event.current_amt
                        to_process_event.current_amt = Decimal(0)

                    # when the oldest active advance was totally cancel move it to the "new" oldest advance,
                    # otherwise move the payment to the next "new" oldest payment to continue try to cancel the
                    # advance amount.
                    if self._old_active_pay_event.current_amt == 0:
                        old_active_pay_id += 1
                    else:
                        old_active_adv_id += 1

                # calculate the new interest payable balance that current debt will generate in future days.
                self._overall_advance_balance += to_process_event.current_amt
                next_event_date = self._get_next_event_date(adv_id=adv_id + 1, pay_id=pay_id)
                old_adv_event_by_date = self._old_adv_event_by_date_dict[to_process_event.event_date]
                old_adv_event_by_date.ipb = Decimal((next_event_date - to_process_event.event_date).days) * \
                    Decimal(self.ACCRUED_INTEREST_CONSTANT * self._overall_advance_balance)
                self._overall_interest_payable_balance += old_adv_event_by_date.ipb

                # move current advance id to the next payment event to be processed in the list.
                adv_id += 1

            # get the next event to be processed.
            to_process_event = self._get_next_event_to_process(adv_id=adv_id, pay_id=pay_id)

        # before statistics calculation ends get Balance Applicable to Future Advances as sum overall
        # payments balance's current amount.
        self._overall_payments_for_future = sum([payment_event.current_amt for payment_event in self._payment_events])

    def _get_next_event_to_process(self, adv_id: int, pay_id: int):
        """
        Get the next event to be processed from given event adv_id and pay_id and next event date.
        In case some or both ids overflow its event arrays respectively, it means no next event to process and
        in that case return None.
        :param adv_id:      id of advance event to check if that event is the next event process or not
        :param pay_id       id of payment event to check if that event is the next event process or not
        :return: BalanceEvent instance with the next event to be processed or None
        """
        next_event_date = self._get_next_event_date(adv_id, pay_id)
        pay_event = None
        if pay_id < len(self._payment_events):
            pay_event = self._payment_events[pay_id]

        adv_event = None
        if adv_id < len(self._advance_events):
            adv_event = self._advance_events[adv_id]

        if pay_event is not None and pay_event.event_date == next_event_date:
            return pay_event
        elif adv_event is not None and adv_event.event_date == next_event_date:
            return adv_event

        return None

    def _get_next_event_date(self, adv_id: int, pay_id: int) -> date:
        """
        Method to get the date of the next event within three following dates:
            - the advance event date (get from self._advance_event[adv_id])
            - the payment event date (get from self._payment_event[adv_id])
            - the instance end_date
        by default return the current instance end_date plus extra date.
        """
        adv_event = None
        if adv_id < len(self._advance_events):
            adv_event = self._advance_events[adv_id]

        pay_event = None
        if pay_id < len(self._payment_events):
            pay_event = self._payment_events[pay_id]

        next_event_date = self._end_date + timedelta(days=1)
        if adv_event is not None and pay_event is not None:
            if adv_event.event_date < pay_event.event_date:
                next_event_date = adv_event.event_date
            elif pay_event.event_date <= self._end_date:
                next_event_date = pay_event.event_date
        elif adv_event is not None:
            if adv_event.event_date <= self._end_date:
                next_event_date = adv_event.event_date
        elif pay_event is not None:
            if pay_event.event_date <= self._end_date:
                next_event_date = pay_event.event_date

        return next_event_date

    def _get_event_records_by_date_from_db(self) -> List[dict]:
        """
        Retrieve event records from database filter them by date created and sort by date_created and type.
        return: list with event db records as a dict with { key: table field name, value: record's field value }
                record ex: {"id": 1, "type": "advance", "amount": 32312, "date_created": "2020-04-21"}
        """
        # query events from database by date and sort by date and type.
        # It uses self._end_date value as upper date limit to filter records.
        with sqlite3.connect(self._db_path) as connection:
            cursor = connection.cursor()
            result = cursor.execute("SELECT * "
                                    "FROM events "
                                    "WHERE date_created <= ?" 
                                    "ORDER BY date_created ASC, type DESC;",
                                    (self._end_date,))

            # convert db event tuples to dictionary by add fields description as a key
            # and their tuple value as dictionary value.
            # ex: {"id": 1, "type": "advance", "amount": 32312, "date_created": "2020-04-21"}
            fields = [field_md[0] for field_md in cursor.description]
            event_records = [dict(zip(fields, row)) for row in result.fetchall()]

            return event_records

    def _init_internal_variables(self):
        """
        Prefetch and set up all the common internal variables that the balance calculator will use to perform statistics
        """
        # init internal advance_events and payment_events lists with BalanceEvents objects
        # build from event records in db.
        event_records = self._get_event_records_by_date_from_db()
        for event_record in event_records:
            balance_event = BalanceEvent(
                event_id=int(event_record["id"]),
                original_amt=Decimal(event_record["amount"]),
                current_amt=Decimal(event_record["amount"]),
                event_date=datetime.strptime(event_record["date_created"], self.DATE_FORMAT).date(),
                event_type=event_record["type"]
            )
            if balance_event.event_type == self.ADVANCE:
                self._advance_events.append(balance_event)

            if balance_event.event_type == self.PAYMENT:
                self._payment_events.append(balance_event)

        # init instance dictionary self._old_adv_event_by_date_dict with advance events as following:
        # - key: advance event date.
        # - value: the oldest advance event object reference for that date.
        for advance_event in self._advance_events:
            if advance_event.event_date not in self._old_adv_event_by_date_dict:
                self._old_adv_event_by_date_dict[advance_event.event_date] = advance_event

        # init the oldest advance and payment events references with the first event elements
        self._old_active_adv_event = self._advance_events[0] if len(self._advance_events) > 0 else None
        self._old_active_ipb_adv_event = self._advance_events[0] if len(self._advance_events) > 0 else None
        self._old_active_pay_event = self._payment_events[0] if len(self._payment_events) > 0 else None

    # Getters methods to retrieve the overall statistics results after calculate_statistics_balance() run.
    def get_advance_events(self) -> List[BalanceEvent]:
        return self._advance_events

    def get_payment_events(self) -> List[BalanceEvent]:
        return self._payment_events

    def get_overall_advance_balance(self) -> Decimal:
        return abs(self._overall_advance_balance)

    def get_overall_interest_payable_balance(self) -> Decimal:
        return abs(self._overall_interest_payable_balance)

    def get_overall_interest_paid(self) -> Decimal:
        return abs(self._overall_interest_paid)

    def get_overall_payments_for_future(self) -> Decimal:
        return abs(self._overall_payments_for_future)

