# server/tools/visualize_grid.py

import cv2
import argparse


def visualize_camera_grid(cam_index=1, rows=3, cols=4, spacing=10):
    """
    Interactive tool to visualize slot grid overlay on camera feed.

    Args:
        cam_index: Camera device index
        rows: Number of rows in grid
        cols: Number of columns in grid  
        spacing: Pixel spacing between cells

    Controls:
        'q' - Quit
        's' - Save current frame with grid
        '+' - Increase spacing
        '-' - Decrease spacing
    """
    cap = cv2.VideoCapture(cam_index, cv2.CAP_DSHOW)

    if not cap.isOpened():
        print(f"Error: Cannot open camera {cam_index}")
        return

    print("Controls:")
    print("  q - Quit")
    print("  s - Save snapshot")
    print("  + - Increase spacing")
    print("  - - Decrease spacing")
    print(f"\nCurrent: {rows}x{cols} grid, {spacing}px spacing")

    while True:
        ret, frame = cap.read()
        if not ret:
            print("Error: Cannot read frame")
            break

        h, w, _ = frame.shape
        cell_h = h // rows
        cell_w = w // cols

        # Draw grid lines
        for i in range(1, rows):
            y = i * cell_h
            cv2.line(frame, (0, y), (w, y), (0, 255, 0), 2)

        for j in range(1, cols):
            x = j * cell_w
            cv2.line(frame, (x, 0), (x, h), (0, 255, 0), 2)

        # Draw cells with spacing
        slot_idx = 0
        for i in range(rows):
            for j in range(cols):
                x1 = j * cell_w + spacing // 2
                y1 = i * cell_h + spacing // 2
                x2 = (j + 1) * cell_w - spacing // 2
                y2 = (i + 1) * cell_h - spacing // 2

                # Draw ROI boundary
                cv2.rectangle(frame, (x1, y1), (x2, y2), (255, 0, 0), 2)

                # Label each slot
                cv2.putText(
                    frame,
                    f"S{slot_idx}",
                    (x1 + 5, y1 + 20),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5,
                    (0, 255, 255),
                    2
                )
                slot_idx += 1

        # Display info overlay
        info_text = f"Grid: {rows}x{cols} | Spacing: {spacing}px | Slots: {rows * cols}"
        cv2.putText(
            frame,
            info_text,
            (10, h - 10),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (255, 255, 255),
            2
        )

        cv2.imshow("Slot Grid Visualization", frame)

        key = cv2.waitKey(1) & 0xFF
        if key == ord('q'):
            break
        elif key == ord('s'):
            filename = f"grid_snapshot_{rows}x{cols}_{spacing}px.jpg"
            cv2.imwrite(filename, frame)
            print(f"Saved: {filename}")
        elif key == ord('+'):
            spacing = min(spacing + 2, 50)
            print(f"Spacing: {spacing}px")
        elif key == ord('-'):
            spacing = max(spacing - 2, 0)
            print(f"Spacing: {spacing}px")

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Visualize camera grid for slot monitoring setup"
    )
    parser.add_argument(
        "--camera",
        type=int,
        default=1,
        help="Camera device index (default: 1)"
    )
    parser.add_argument(
        "--rows",
        type=int,
        default=5,
        help="Number of rows (default: 5)"
    )
    parser.add_argument(
        "--cols",
        type=int,
        default=10,
        help="Number of columns (default: 10)"
    )
    parser.add_argument(
        "--spacing",
        type=int,
        default=10,
        help="Spacing between cells in pixels (default: 10)"
    )

    args = parser.parse_args()

    visualize_camera_grid(
        cam_index=args.camera,
        rows=args.rows,
        cols=args.cols,
        spacing=args.spacing
    )