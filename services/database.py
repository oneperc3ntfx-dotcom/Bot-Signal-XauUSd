import os
import psycopg2


DATABASE_URL = os.getenv(
    "DATABASE_URL"
)


def save_signal(text):

    try:

        conn = psycopg2.connect(
            DATABASE_URL
        )

        cur = conn.cursor()


        cur.execute(
            """
            INSERT INTO signals(signal)
            VALUES(%s)
            """,
            (
                text,
            )
        )


        conn.commit()


        cur.close()
        conn.close()


        print(
            "SIGNAL SAVED DATABASE"
        )


    except Exception as e:

        print(
            "DATABASE ERROR:",
            e
        )
