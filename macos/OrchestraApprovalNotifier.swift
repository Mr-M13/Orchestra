import Cocoa
import Foundation
import UserNotifications

private let categoryIdentifier = "ORCHESTRA_APPROVAL"
private let approveAction = "APPROVE"
private let rejectAction = "REJECT"
private let detailsAction = "DETAILS"

private struct NotificationSpec: Codable {
    let notification_id: String
    let task_id: String
    let title: String
    let repository: String
    let worker: String
    let body: String
    let portal_base_url: String
    let portal_token_path: String
    let receipt_path: String
}

final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    private let center = UNUserNotificationCenter.current()

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        center.delegate = self
        let actions = [
            UNNotificationAction(identifier: approveAction, title: "Approve", options: [.foreground]),
            UNNotificationAction(identifier: rejectAction, title: "Reject", options: [.foreground, .destructive]),
            UNNotificationAction(identifier: detailsAction, title: "View details", options: [.foreground]),
        ]
        center.setNotificationCategories([
            UNNotificationCategory(
                identifier: categoryIdentifier, actions: actions,
                intentIdentifiers: [], options: [.customDismissAction]
            )
        ])

        let arguments = CommandLine.arguments
        if arguments.count == 3 && arguments[1] == "--notify" {
            deliver(specPath: arguments[2])
        } else if arguments.count == 3 && arguments[1] == "--remove" {
            let identifier = arguments[2]
            center.removePendingNotificationRequests(withIdentifiers: [identifier])
            center.removeDeliveredNotifications(withIdentifiers: [identifier])
            terminate(0)
        } else if arguments.count > 1 {
            fputs("usage: OrchestraApprovalNotifier --notify SPEC | --remove ID\n", stderr)
            terminate(2)
        }
        // With no arguments the notification center launched the app to handle a response.
    }

    private func deliver(specPath: String) {
        do {
            let data = try Data(contentsOf: URL(fileURLWithPath: specPath))
            let spec = try JSONDecoder().decode(NotificationSpec.self, from: data)
            center.requestAuthorization(options: [.alert, .sound]) { granted, error in
                guard error == nil && granted else {
                    fputs("notification authorization is unavailable\n", stderr)
                    self.terminate(3)
                    return
                }
                let content = UNMutableNotificationContent()
                content.title = spec.title
                content.subtitle = "\(spec.repository) · \(spec.worker)"
                content.body = spec.body
                content.sound = .default
                content.categoryIdentifier = categoryIdentifier
                content.userInfo = [
                    "task_id": spec.task_id,
                    "portal_base_url": spec.portal_base_url,
                    "portal_token_path": spec.portal_token_path,
                ]
                let request = UNNotificationRequest(
                    identifier: spec.notification_id, content: content, trigger: nil
                )
                self.center.add(request) { addError in
                    if addError != nil {
                        fputs("notification delivery failed\n", stderr)
                        self.terminate(4)
                    } else {
                        do {
                            try (spec.notification_id + "\n").write(
                                toFile: spec.receipt_path, atomically: true, encoding: .utf8
                            )
                            try FileManager.default.setAttributes(
                                [.posixPermissions: 0o600], ofItemAtPath: spec.receipt_path
                            )
                            self.terminate(0)
                        } catch {
                            fputs("notification receipt failed\n", stderr)
                            self.terminate(6)
                        }
                    }
                }
            }
        } catch {
            fputs("invalid notification specification\n", stderr)
            terminate(2)
        }
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .list, .sound])
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        defer { completionHandler() }
        let info = response.notification.request.content.userInfo
        guard
            let taskID = info["task_id"] as? String,
            let baseURL = info["portal_base_url"] as? String,
            let tokenPath = info["portal_token_path"] as? String,
            let token = try? String(contentsOfFile: tokenPath, encoding: .utf8)
                .trimmingCharacters(in: .whitespacesAndNewlines),
            token.count == 64
        else {
            terminate(5)
            return
        }
        let intent: String
        switch response.actionIdentifier {
        case approveAction: intent = "approve"
        case rejectAction: intent = "reject"
        default: intent = "details"
        }
        guard var components = URLComponents(string: baseURL) else {
            terminate(5)
            return
        }
        let basePath = components.path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        components.path = "/" + [basePath, "bootstrap", token, "task", taskID]
            .filter { !$0.isEmpty }.joined(separator: "/")
        components.queryItems = [URLQueryItem(name: "intent", value: intent)]
        guard let url = components.url else {
            terminate(5)
            return
        }
        NSWorkspace.shared.open(url)
        center.removeDeliveredNotifications(withIdentifiers: [response.notification.request.identifier])
        terminate(0)
    }

    private func terminate(_ code: Int32) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
            exit(code)
        }
    }
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.run()
