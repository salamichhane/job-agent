import Foundation
import PDFKit

guard CommandLine.arguments.count >= 2 else {
    FileHandle.standardError.write(Data("Usage: extract_pdf_text <pdf-path>\n".utf8))
    exit(2)
}

let path = CommandLine.arguments[1]
let url = URL(fileURLWithPath: path)

guard let document = PDFDocument(url: url) else {
    exit(1)
}

var pages: [String] = []
for index in 0..<document.pageCount {
    if let page = document.page(at: index), let text = page.string {
        pages.append(text)
    }
}

print(pages.joined(separator: "\n\n"))
