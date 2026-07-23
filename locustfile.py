from locust import HttpUser, task, between

class APIUser(HttpUser):
    wait_time = between(1, 3)

    @task
    def check_balance(self):
        self.client.post(
            "/check_balance",
            json={
                "firstname": "John",
                "lastname": "Doe",
                "email": "johndoe@example.com",
            }
        )