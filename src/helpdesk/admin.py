"""Operator tool for the helpdesk's real records.

    python -m helpdesk.admin add-customer --tenant acme --name "Ada Lovelace" --email ada@acme.com
    python -m helpdesk.admin open-ticket --tenant acme --customer C-1A2B3C4D --order-amount 129.99 \\
        --subject "Item arrived damaged" --body "The box was crushed in transit."
    python -m helpdesk.admin list --tenant acme

A production helpdesk starts empty: no seeded or synthetic records. Customers and the orders
their tickets are about come from here (or from your own intake system writing to the same
database); agents then read tickets and act on them through AgentGuard. Reads
HELPDESK_DATABASE_URL like the service itself.
"""

import argparse
import sys
import uuid

from sqlalchemy.orm import Session

from helpdesk.app import SessionLocal, new_ticket_id
from helpdesk.models import Customer, Refund, Ticket


def add_customer(session: Session, tenant: str, name: str, email: str) -> Customer:
    customer = Customer(id=f"C-{uuid.uuid4().hex[:8].upper()}", tenant_id=tenant, name=name, email=email)
    session.add(customer)
    session.commit()
    return customer


def open_ticket(session: Session, tenant: str, subject: str, body: str, *, customer_id: str | None,
                order_amount: float) -> Ticket:
    if order_amount < 0:
        raise ValueError("order amount can't be negative")
    if customer_id:
        customer = session.get(Customer, customer_id)
        if customer is None or customer.tenant_id != tenant:
            raise ValueError(f"no customer {customer_id} in tenant {tenant}")
    ticket = Ticket(id=new_ticket_id(), tenant_id=tenant, customer_id=customer_id, subject=subject, body=body,
                    order_amount=order_amount)
    session.add(ticket)
    session.commit()
    return ticket


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage the helpdesk's customers and tickets")
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("add-customer")
    c.add_argument("--tenant", required=True)
    c.add_argument("--name", required=True)
    c.add_argument("--email", required=True)
    t = sub.add_parser("open-ticket")
    t.add_argument("--tenant", required=True)
    t.add_argument("--subject", required=True)
    t.add_argument("--body", default="")
    t.add_argument("--customer")
    t.add_argument("--order-amount", type=float, default=0.0, help="what the customer paid; caps refunds")
    ls = sub.add_parser("list")
    ls.add_argument("--tenant", required=True)
    args = parser.parse_args(argv)

    with SessionLocal() as session:
        try:
            if args.command == "add-customer":
                customer = add_customer(session, args.tenant, args.name, args.email)
                print(f"customer {customer.id} ({customer.name}) in {customer.tenant_id}")
            elif args.command == "open-ticket":
                ticket = open_ticket(session, args.tenant, args.subject, args.body,
                                     customer_id=args.customer, order_amount=args.order_amount)
                print(f"ticket {ticket.id} opened for {ticket.tenant_id} (order {ticket.order_amount:.2f})")
            else:
                for ticket in session.query(Ticket).filter(Ticket.tenant_id == args.tenant).order_by(Ticket.created_at):
                    refunded = sum(r.amount for r in session.query(Refund).filter(Refund.ticket_id == ticket.id))
                    print(f"{ticket.id}  {ticket.status:<7} order {ticket.order_amount:>9.2f}  "
                          f"refunded {refunded:>9.2f}  {ticket.subject}")
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
