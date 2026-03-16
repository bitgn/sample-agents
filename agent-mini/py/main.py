from harness.agent_mini.agent_mini_connect import AgentMiniHarnessClientSync


def main():
    print("Hello from python!")


    harness = AgentMiniHarnessClientSync(address="http://127.0.0.1:8080")



if __name__ == "__main__":
    main()
