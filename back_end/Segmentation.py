import cv2

# grid size
rows, cols = 3, 4  # x rows, y cols
spacing = 10       # pixels between cells

cap = cv2.VideoCapture(0)

while True:
    ret, frame = cap.read()
    if not ret:
        break

    h, w, _ = frame.shape
    cell_h = h // rows
    cell_w = w // cols

    # draw grid lines with spacing accounted
    for i in range(1, rows):
        y = i * cell_h
        cv2.line(frame, (0, y), (w, y), (0, 255, 0), 1)
    for j in range(1, cols):
        x = j * cell_w
        cv2.line(frame, (x, 0), (x, h), (0, 255, 0), 1)

    # show frame
    cv2.imshow("Grid", frame)

    # iterate over cells with spacing
    for i in range(rows):
        for j in range(cols):
            x1 = j * cell_w + spacing // 2
            y1 = i * cell_h + spacing // 2
            x2 = (j + 1) * cell_w - spacing // 2
            y2 = (i + 1) * cell_h - spacing // 2

            cell = frame[y1:y2, x1:x2]

            # optional: draw rectangle around cell to visualize spacing
            cv2.rectangle(frame, (x1, y1), (x2, y2), (255, 0, 0), 1)

    cv2.imshow("Grid with Spacing", frame)

    if cv2.waitKey(1) & 0xFF == ord("q"):
        break

cap.release()
cv2.destroyAllWindows()
